import io
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zipfile import ZipFile, ZipInfo

from bot.session_uploads import UploadError, stage_sessions
from bot import handlers


def archive_bytes(entries):
    stream = io.BytesIO()
    with ZipFile(stream, 'w') as archive:
        for name, content in entries:
            archive.writestr(name, content)
    stream.seek(0)
    return stream


class StagingTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.directory = Path(tmp.name)

    def test_nested_sessions_and_other_files(self):
        files = stage_sessions(archive_bytes([
            ('accounts/one.session', b'one'), ('two.SESSION', b'two'),
            ('notes.txt', b'ignore'), ('__MACOSX/._three.session', b'ignore'),
        ]), 'sessions.zip', self.directory)
        self.assertEqual({p.name: p.read_bytes() for p in files},
                         {'one.session': b'one', 'two.session': b'two'})
        self.assertTrue(all(p.stat().st_mode & 0o777 == 0o600 for p in files))

    def test_single_session_remains_supported(self):
        files = stage_sessions(io.BytesIO(b'one'), 'one.session', self.directory)
        self.assertEqual(files[0].read_bytes(), b'one')

    def test_archive_paths_are_rejected_before_any_files_are_staged(self):
        for name in ('../escape.session', '/absolute.session', 'C:/win.session', '..\\escape.session'):
            with self.subTest(name=name), self.assertRaises(UploadError):
                stage_sessions(archive_bytes([('ok.session', b'ok'), (name, b'bad')]),
                               'sessions.zip', self.directory)
            self.assertEqual(list(self.directory.iterdir()), [])

    def test_duplicate_basename_is_rejected_before_staging(self):
        with self.assertRaises(UploadError):
            stage_sessions(archive_bytes([('a/one.session', b'a'), ('b/one.session', b'b')]),
                           'sessions.zip', self.directory)
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_invalid_empty_and_symlink_archives(self):
        link = ZipInfo('link.session')
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        for stream in (io.BytesIO(b'not a zip'), archive_bytes([('notes.txt', b'none')]),
                       archive_bytes([(link, b'/etc/passwd')])):
            with self.assertRaises(UploadError):
                stage_sessions(stream, 'sessions.zip', self.directory)

    def test_size_and_entry_limits(self):
        with patch('bot.session_uploads.MAX_SESSION_BYTES', 2):
            with self.assertRaises(UploadError):
                stage_sessions(archive_bytes([('one.session', b'123')]), 'sessions.zip', self.directory)
            with self.assertRaises(UploadError):
                stage_sessions(io.BytesIO(b'123'), 'one.session', self.directory)
        with patch('bot.session_uploads.MAX_ARCHIVE_ENTRIES', 1):
            with self.assertRaises(UploadError):
                stage_sessions(archive_bytes([('one.session', b'1'), ('two.session', b'2')]),
                               'sessions.zip', self.directory)

    def test_single_session_cannot_escape_directory(self):
        with self.assertRaises(UploadError):
            stage_sessions(io.BytesIO(b'one'), '../one.session', self.directory)


class UploadHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.directory = Path(tmp.name)
        self.message = SimpleNamespace(from_user=object(), answer=AsyncMock(),
                                       document=SimpleNamespace(file_name='sessions.zip', file_size=100))
        self.bot = AsyncMock()
        self.db = AsyncMock()
        self.db.add_session.return_value = True
        for target, value in (
            ('bot.handlers.SESSIONS_DIR', self.directory),
            ('bot.handlers.check_admin', AsyncMock(return_value=True)),
            ('bot.handlers.db', self.db),
        ):
            p = patch(target, value)
            p.start()
            self.addCleanup(p.stop)

    def upload(self, content):
        async def download(doc, destination):
            destination.write(content)
        self.bot.download.side_effect = download

    async def test_archive_adds_new_sessions_without_overwriting_existing(self):
        (self.directory / 'old.session').write_bytes(b'keep')
        self.upload(archive_bytes([('old.session', b'replace'), ('folder/new.session', b'new')]).getvalue())
        await handlers.handle_session_file(self.message, self.bot)
        self.assertEqual((self.directory / 'old.session').read_bytes(), b'keep')
        self.assertEqual((self.directory / 'new.session').read_bytes(), b'new')
        self.db.add_session.assert_awaited_once_with('new')
        self.assertEqual(sorted(p.name for p in self.directory.iterdir()), ['new.session', 'old.session'])
        self.assertIn('Добавлено в очередь: 1', self.message.answer.call_args.args[0])
        self.assertIn('пропущено: 1', self.message.answer.call_args.args[0])

    async def test_individual_session_still_adds_to_queue(self):
        self.message.document.file_name = 'one.session'
        self.upload(b'one')
        await handlers.handle_session_file(self.message, self.bot)
        self.db.add_session.assert_awaited_once_with('one')

    async def test_rejected_archive_never_changes_live_sessions(self):
        self.upload(archive_bytes([('ok.session', b'ok'), ('../bad.session', b'bad')]).getvalue())
        await handlers.handle_session_file(self.message, self.bot)
        self.assertEqual(list(self.directory.iterdir()), [])
        self.db.add_session.assert_not_awaited()
        self.assertIn('отклонена', self.message.answer.call_args.args[0])

    async def test_crc_failure_does_not_install_partially_staged_archive(self):
        content = archive_bytes([('first.session', b'first'), ('second.session', b'good-data')]).getvalue()
        self.upload(content.replace(b'good-data', b'bad--data', 1))
        await handlers.handle_session_file(self.message, self.bot)
        self.assertEqual(list(self.directory.iterdir()), [])
        self.db.add_session.assert_not_awaited()
        self.assertIn('отклонена', self.message.answer.call_args.args[0])

    async def test_unauthorized_user_does_not_download(self):
        with patch('bot.handlers.check_admin', AsyncMock(return_value=False)):
            await handlers.handle_session_file(self.message, self.bot)
        self.bot.download.assert_not_awaited()

    async def test_missing_filename_is_rejected(self):
        self.message.document.file_name = None
        await handlers.handle_session_file(self.message, self.bot)
        self.bot.download.assert_not_awaited()
