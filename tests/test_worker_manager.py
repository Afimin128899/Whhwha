import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from database.db import Database
from worker.manager import WorkerManager


class WorkerConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(Path(self.tmp.name) / 'test.sqlite3')
        await self.db.init()
        await self.db.set_setting('delay_min', '0')
        await self.db.set_setting('delay_max', '0')
        for i in range(12):
            await self.db.add_session(str(i))
        self.bot = AsyncMock()
        self.manager = WorkerManager(self.bot)
        self.db_patch = patch('worker.manager.db', self.db)
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)
        self.admin_patch = patch('worker.manager.config.ADMIN_ID', 123)
        self.admin_patch.start()
        self.addCleanup(self.admin_patch.stop)
        self.env_patch = patch.dict('os.environ', {'CHECK_RECEIVER_ENABLED': '0'})
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        self.addAsyncCleanup(self.manager.stop)

    async def test_five_concurrent_unique_sessions_and_one_completion(self):
        active = 0
        peak = 0
        seen = []
        full = asyncio.Event()
        release = asyncio.Event()

        async def process(session_name, **kwargs):
            nonlocal active, peak
            seen.append(session_name)
            active += 1
            peak = max(peak, active)
            if active == 5:
                full.set()
            try:
                await release.wait()
                return 'DONE', 'ok', None
            finally:
                active -= 1

        with patch('worker.manager.process_account_telethon', process):
            self.assertTrue(await self.manager.start())
            await asyncio.wait_for(full.wait(), 5)
            self.assertFalse(await self.manager.start())
            self.assertEqual(len(seen), 5)
            self.bot.send_message.assert_not_awaited()
            release.set()
            await asyncio.wait_for(self.manager._task, 5)
        self.assertEqual(peak, 5)
        self.assertEqual(len(seen), 12)
        self.assertEqual(len(set(seen)), 12)
        self.assertEqual((await self.db.get_sessions_stats())['DONE'], 12)
        self.bot.send_message.assert_awaited_once()
        self.assertFalse(self.manager.is_running())

    async def test_stop_waits_for_cleanup_and_resets_claimed_sessions(self):
        started = 0
        closed = []
        full = asyncio.Event()

        async def process(session_name, **kwargs):
            nonlocal started
            started += 1
            if started == 5:
                full.set()
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0.01)
                closed.append(session_name)

        with patch('worker.manager.process_account_telethon', process):
            await self.manager.start()
            await asyncio.wait_for(full.wait(), 5)
            await asyncio.wait_for(self.manager.stop(), 5)
        self.assertEqual(len(closed), 5)
        stats = await self.db.get_sessions_stats()
        self.assertEqual(stats['PROCESSING'], 0)
        self.assertEqual(stats['READY'], 12)
        self.bot.send_message.assert_not_awaited()
        self.assertEqual(await self.db.get_setting('is_running'), '0')

    async def test_worker_failure_cleans_up_group(self):
        async def process(**kwargs):
            raise RuntimeError('test failure')

        with patch('worker.manager.process_account_telethon', process):
            with self.assertLogs('worker_manager', level='ERROR'):
                await self.manager.start()
                await asyncio.wait_for(self.manager._task, 5)
        self.assertFalse(self.manager.is_running())
        self.assertEqual((await self.db.get_sessions_stats())['PROCESSING'], 0)
        self.bot.send_message.assert_awaited_once()
        self.assertIn('Воркер остановлен', self.bot.send_message.call_args.args[1])

    async def test_receiver_opens_checks_and_closes_after_workers(self):
        receiver = AsyncMock()
        receiver.open_check.return_value = 'Ссылка открыта получателем.'

        async def process(session_name, **kwargs):
            return 'DONE', 'ok', 'https://t.me/OnoGo_bot?start=' + session_name

        with patch.dict('os.environ', {'CHECK_RECEIVER_ENABLED': '1'}):
            with patch('worker.manager.CheckReceiver', return_value=receiver):
                with patch('worker.manager.process_account_telethon', process):
                    await self.manager.start()
                    await asyncio.wait_for(self.manager._task, 10)
        receiver.connect.assert_awaited_once()
        self.assertEqual(receiver.open_check.await_count, 12)
        receiver.close.assert_awaited_once()
        self.assertIsNone(self.manager._receiver)

    async def test_invalid_receiver_leaves_queue_untouched(self):
        receiver = AsyncMock()
        receiver.connect.side_effect = RuntimeError('not authorized')
        with patch.dict('os.environ', {'CHECK_RECEIVER_ENABLED': '1'}):
            with patch('worker.manager.CheckReceiver', return_value=receiver):
                with self.assertLogs('worker_manager', level='ERROR'):
                    await self.manager.start()
                    await asyncio.wait_for(self.manager._task, 5)
        receiver.close.assert_awaited_once()
        receiver.open_check.assert_not_awaited()
        self.assertEqual((await self.db.get_sessions_stats())['READY'], 12)
        self.assertFalse(self.manager.is_running())

    async def test_progress_every_ten_successfully_sent_checks(self):
        for i in range(12, 25):
            await self.db.add_session(str(i))

        async def process(session_name, **kwargs):
            return 'DONE', 'ok', 'https://t.me/example?start=' + session_name

        with patch('worker.manager.process_account_telethon', process):
            await self.manager.start()
            await asyncio.wait_for(self.manager._task, 10)
        progress = [call.args[1] for call in self.bot.send_message.await_args_list
                    if call.args[1].startswith('Отправлено ')]
        self.assertEqual(progress, ['Отправлено 10 чеков.', 'Отправлено 20 чеков.'])
        self.assertEqual(self.manager._sent_checks, 25)
        # A new worker run starts its own progress counter.
        await self.manager.start()
        await asyncio.wait_for(self.manager._task, 5)
        self.assertEqual(self.manager._sent_checks, 0)

    async def test_failed_sessions_and_deliveries_do_not_count(self):
        async def process(session_name, **kwargs):
            if session_name in ('0', '1'):
                return 'ERROR', 'no check', None
            return 'DONE', 'ok', 'https://t.me/example?start=' + session_name

        failed = False

        async def send_message(admin, text, **kwargs):
            nonlocal failed
            if 'Новый чек' in text and not failed:
                failed = True
                raise RuntimeError('delivery failed')

        self.bot.send_message.side_effect = send_message
        with patch('worker.manager.process_account_telethon', process):
            with self.assertLogs('worker_manager', level='ERROR'):
                await self.manager.start()
                await asyncio.wait_for(self.manager._task, 10)
        self.assertEqual(self.manager._sent_checks, 9)
        progress = [call.args[1] for call in self.bot.send_message.await_args_list
                    if call.args[1].startswith('Отправлено ')]
        self.assertEqual(progress, [])
