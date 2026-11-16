import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

import aiosqlite

from worker.check_receiver import CheckReceiver, parse_check_link


class CheckLinkTests(unittest.TestCase):
    def test_expected_bot_and_payload(self):
        self.assertEqual(parse_check_link('https://t.me/OnoGo_bot?start=check_123-A'),
                         'check_123-A')

    def test_selected_bot_from_actual_link(self):
        self.assertEqual(parse_check_link('https://t.me/send?start=abc', 'send'), 'abc')
        with self.assertRaises(ValueError):
            parse_check_link('https://t.me/send?start=abc', 'OnoGo_bot')

    def test_other_targets_and_invalid_payloads_are_rejected(self):
        for url in (
            'https://t.me/other_bot?start=check',
            'https://t.me.evil/OnoGo_bot?start=check',
            'https://user@t.me/OnoGo_bot?start=check',
            'https://t.me/OnoGo_bot?start=a&start=b',
            'https://t.me/OnoGo_bot?start=%0Acommand',
            'https://t.me/OnoGo_bot?start=',
            'https://t.me/OnoGo_bot?start=x&other=y',
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                parse_check_link(url)


class ReceiverTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.receiver = CheckReceiver()
        self.receiver.client = AsyncMock()
        self.receiver.bot = object()
        self.receiver.ledger = await aiosqlite.connect(Path(tmp.name) / 'ledger.sqlite3')
        await self.receiver.ledger.execute(
            'CREATE TABLE opened_checks (payload TEXT PRIMARY KEY, state TEXT NOT NULL)')
        await self.receiver.ledger.commit()
        self.addAsyncCleanup(self.receiver.close)

    async def test_duplicate_links_only_open_once(self):
        url = 'https://t.me/OnoGo_bot?start=abc'
        results = await asyncio.gather(*(self.receiver.open_check(url) for _ in range(5)))
        self.receiver.client.assert_awaited_once()
        request = self.receiver.client.call_args.args[0]
        self.assertEqual(request.start_param, 'abc')
        self.assertEqual(results.count('Ссылка уже отправлялась получателем.'), 4)

    async def test_recipient_requests_are_serialized(self):
        active = peak = 0

        async def send(request):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            active -= 1

        self.receiver.client.side_effect = send
        await asyncio.gather(*(self.receiver.open_check(
            f'https://t.me/OnoGo_bot?start=check{i}') for i in range(5)))
        self.assertEqual(peak, 1)
        self.assertEqual(self.receiver.client.await_count, 5)

    async def test_uncertain_send_is_not_blindly_retried(self):
        self.receiver.client.side_effect = OSError('connection lost')
        url = 'https://t.me/OnoGo_bot?start=abc'
        with self.assertRaises(OSError):
            await self.receiver.open_check(url)
        self.assertIn('неизвестен', await self.receiver.open_check(url))
        self.receiver.client.assert_awaited_once()

    async def test_same_payload_in_different_bots_is_independent(self):
        await self.receiver.open_check('https://t.me/OnoGo_bot?start=abc')
        self.receiver.bot_username = 'send'
        await self.receiver.open_check('https://t.me/send?start=abc')
        self.assertEqual(self.receiver.client.await_count, 2)

    async def test_legacy_journal_entry_prevents_duplicate_opening(self):
        await self.receiver.ledger.execute("INSERT INTO opened_checks VALUES ('abc', 'SENT')")
        await self.receiver.ledger.commit()
        self.assertIn('уже отправлялась', await self.receiver.open_check(
            'https://t.me/OnoGo_bot?start=abc'))
        self.receiver.client.assert_not_awaited()
