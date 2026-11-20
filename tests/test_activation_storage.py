import sqlite3
import tempfile
import unittest
from pathlib import Path

from database.db import Database


class ActivationStorageTests(unittest.IsolatedAsyncioTestCase):
    async def test_old_checks_survive_idempotent_migration_and_status_update(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'old.sqlite3'
            with sqlite3.connect(path) as connection:
                connection.execute('CREATE TABLE checks (id INTEGER PRIMARY KEY, session_name TEXT, check_url TEXT, amount REAL, created_at TEXT)')
                connection.execute("INSERT INTO checks VALUES (1, 'one', 'https://t.me/send?start=abc', 0.1, 'old')")
            db = Database(path)
            await db.init()
            await db.init()
            checks = await db.get_all_checks()
            self.assertEqual(len(checks), 1)
            self.assertEqual(checks[0]['activation_note'], 'Активация не проверялась.')
            await db.set_check_activation('one', checks[0]['check_url'], '✅ Чек активирован.')
            self.assertEqual((await Database(path).get_all_checks())[0]['activation_note'], '✅ Чек активирован.')
            self.assertEqual(await db.get_setting('sequential_accounts'), '0')

    async def test_existing_sequential_mode_migrates_once_to_five_parallel_workers(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'old.sqlite3'
            with sqlite3.connect(path) as connection:
                connection.execute('CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
                connection.executemany('INSERT INTO settings VALUES (?, ?)', [
                    ('worker_count', '10'), ('sequential_accounts', '1')])
            db = Database(path)
            await db.init()
            self.assertEqual(await db.get_setting('worker_count'), '5')
            self.assertEqual(await db.get_setting('sequential_accounts'), '0')
            await db.set_setting('worker_count', '10')
            await db.set_setting('sequential_accounts', '1')
            await db.init()
            self.assertEqual(await db.get_setting('worker_count'), '10')
            self.assertEqual(await db.get_setting('sequential_accounts'), '1')

    async def test_bulk_session_import_preserves_duplicates_and_existing_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / 'sessions.sqlite3')
            await db.init()
            await db.add_session('existing')
            await db.update_session_status('existing', 'DONE', 'finished')
            self.assertEqual(await db.add_sessions(['existing', 'one', 'two', 'one']), 2)
            self.assertEqual(await db.add_sessions([]), 0)
            stats = await db.get_sessions_stats()
            self.assertEqual(stats['DONE'], 1)
            self.assertEqual(stats['READY'], 2)

    async def test_repeated_history_check_does_not_duplicate_or_overwrite_amount(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / 'checks.sqlite3')
            await db.init()
            url = 'https://t.me/send?start=one'
            await db.add_check('one', url, 0.5)
            await db.add_check('one', url, 0.0)
            checks = await db.get_all_checks()
            self.assertEqual(len(checks), 1)
            self.assertEqual(checks[0]['amount'], 0.5)
