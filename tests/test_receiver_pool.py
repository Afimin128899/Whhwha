import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiosqlite
from telethon.errors import FloodWaitError

from worker.check_receiver import CheckReceiver, ReceiverPool


class ReceiverPoolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for slot in range(1, 6):
            name = 'recipient.session' if slot == 1 else f'recipient_{slot}.session'
            (self.root / name).touch()
        self.now = 0.0
        self.sleeps = []
        self.sent = []
        self.children = {}
        self.real_connect = CheckReceiver.connect

        async def connect(child):
            slot = 1 if child.session_path.stem == 'recipient' else int(child.session_path.stem.split('_')[1])
            child.client = AsyncMock()
            child.client.get_me.return_value = SimpleNamespace(id=100 + slot, bot=False)
            child.client.get_messages.return_value = []
            child.bot = object()
            child._clock = lambda: self.now
            async def sleep(seconds):
                self.sleeps.append(seconds)
                self.now += seconds
            child._sleep = sleep
            async def send(request):
                self.sent.append((slot, request.start_param, self.now))
                return SimpleNamespace(updates=[])
            child.client.side_effect = send
            child._observe_activation = AsyncMock(return_value=('ACTIVATED', '✅ Чек активирован.'))
            child.ledger = await aiosqlite.connect(self.root / 'opened.sqlite3')
            await child.ledger.execute('CREATE TABLE IF NOT EXISTS opened_checks (payload TEXT PRIMARY KEY, state TEXT NOT NULL)')
            await child.ledger.commit()
            self.children[slot] = child

        for target, value in (
            ('worker.check_receiver.RECEIVER_DIR', self.root),
            ('worker.check_receiver.CheckReceiver.connect', connect),
        ):
            patcher = patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.pool = await self.make_pool()
        self.addAsyncCleanup(self.pool.close)

    async def make_pool(self):
        pool = ReceiverPool()
        pool._clock = lambda: self.now
        pool._wall_clock = lambda: self.now + 1000
        await pool.connect()
        return pool

    async def test_five_accounts_round_robin_with_global_ten_second_interval(self):
        notes = await asyncio.gather(*(self.pool.open_check(
            f'https://t.me/OnoGo_bot?start=check{i}') for i in range(7)))
        self.assertEqual([s for s, _, _ in self.sent], [1, 2, 3, 4, 5, 1, 2])
        self.assertEqual([t for _, _, t in self.sent], [0, 10, 20, 30, 40, 50, 60])
        self.assertIn('Получатель №2, Telegram ID 102', notes[-1])

    async def test_duplicate_and_restart_keep_recipient_and_cursor(self):
        url = 'https://t.me/OnoGo_bot?start=one'
        await self.pool.open_check(url)
        await self.pool.open_check(url)
        self.assertEqual(len(self.sent), 1)
        await self.pool.close()
        pool = await self.make_pool()
        try:
            await pool.open_check(url)
            self.assertEqual(len(self.sent), 1)
            await pool.open_check('https://t.me/OnoGo_bot?start=two')
            self.assertEqual(self.sent[-1], (2, 'two', 10))
        finally:
            await pool.close()

    async def test_floodwait_stays_on_assigned_account_and_blocks_other_accounts(self):
        child = self.children[1]
        original = child.client.side_effect
        attempts = 0
        async def send(request):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise FloodWaitError(None, capture=25)
            return await original(request)
        child.client.side_effect = send
        await asyncio.gather(
            self.pool.open_check('https://t.me/OnoGo_bot?start=wait'),
            self.pool.open_check('https://t.me/OnoGo_bot?start=next'),
        )
        self.assertEqual(self.sent, [(1, 'wait', 26), (2, 'next', 36)])

    async def test_cancelled_floodwait_and_restart_preserve_global_cooldown(self):
        self.children[1].client.side_effect = FloodWaitError(None, capture=25)
        async def cancel(seconds):
            raise asyncio.CancelledError()
        self.children[1]._sleep = cancel
        with self.assertRaises(asyncio.CancelledError):
            await self.pool.open_check('https://t.me/OnoGo_bot?start=wait')
        await self.pool.close()
        pool = await self.make_pool()
        try:
            await pool.open_check('https://t.me/OnoGo_bot?start=wait')
            self.assertEqual(self.sent, [(1, 'wait', 26)])
        finally:
            await pool.close()

    async def test_uncertain_send_never_moves_to_another_account(self):
        self.children[1].client.side_effect = OSError('offline')
        url = 'https://t.me/OnoGo_bot?start=uncertain'
        with self.assertRaises(OSError):
            await self.pool.open_check(url)
        self.assertIn('неизвестен', await self.pool.open_check(url))
        self.children[2].client.assert_not_awaited()
        self.pool.account_ids[1] = 999
        self.assertIn('заменён', await self.pool.open_check(url))
        self.children[2].client.assert_not_awaited()

    async def test_invalid_recipient_fails_group_and_closes_every_client(self):
        await self.pool.close()
        async def fail(child):
            child.client = AsyncMock()
            self.children[1] = child
            raise RuntimeError('invalid session')
        pool = ReceiverPool()
        with patch('worker.check_receiver.CheckReceiver.connect', fail):
            with self.assertRaises(RuntimeError):
                await pool.connect()
        self.children[1].client.disconnect.assert_awaited_once()
        self.assertEqual(pool.receivers, {})

    async def test_sparse_slots_are_used_and_empty_slots_skipped(self):
        await self.pool.close()
        for slot in (2, 4, 5):
            (self.root / f'recipient_{slot}.session').unlink()
        pool = await self.make_pool()
        try:
            for i in range(3):
                await pool.open_check(f'https://t.me/OnoGo_bot?start=sparse{i}')
            self.assertEqual([slot for slot, _, _ in self.sent], [1, 3, 1])
        finally:
            await pool.close()

    async def test_read_floodwait_also_delays_the_next_recipient(self):
        child = self.children[1]
        # Use the real observer; the initial baseline read succeeds, observation is limited.
        child._observe_activation = CheckReceiver._observe_activation.__get__(child)
        child.client.get_messages.side_effect = [[], FloodWaitError(None, capture=25)]
        await self.pool.open_check('https://t.me/OnoGo_bot?start=readwait')
        await self.pool.open_check('https://t.me/OnoGo_bot?start=afterread')
        self.assertEqual(self.sent, [(1, 'readwait', 0), (2, 'afterread', 26)])
