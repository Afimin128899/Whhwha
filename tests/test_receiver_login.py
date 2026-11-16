import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from telethon.errors import SessionPasswordNeededError, PhoneCodeExpiredError

from bot import handlers as h
from bot.states import AddSessionStates


class ReceiverLoginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.recipient = self.root / 'recipient.session'
        self.recipient.write_bytes(b'old-recipient')
        self.state = FSMContext(storage=MemoryStorage(), key=StorageKey(
            bot_id=1, chat_id=123, user_id=123))
        self.user = SimpleNamespace(id=123, username=None)
        self.chat = SimpleNamespace(type='private')
        self.message = SimpleNamespace(from_user=self.user, chat=self.chat,
                                       text='', answer=AsyncMock(), delete=AsyncMock())
        self.callback = SimpleNamespace(from_user=self.user, message=self.message,
                                        answer=AsyncMock())
        self.manager = AsyncMock()
        self.manager.is_running.return_value = False
        self.db = AsyncMock()
        self.db.get_setting.return_value = ''
        self.client = AsyncMock()
        self.client.get_me.return_value = SimpleNamespace(id=999, bot=False)
        self.client.send_code_request.return_value = SimpleNamespace(phone_code_hash='hash')
        self.client_factory = lambda *args, **kwargs: self.make_client(*args, **kwargs)
        for target, value in (
            ('bot.handlers.config.ADMIN_ID', 123),
            ('bot.handlers.RECEIVER_DIR', self.root),
            ('bot.handlers.RECEIVER_SESSION', self.recipient),
            ('bot.handlers.db', self.db),
            ('bot.handlers.get_random_api_credentials', lambda: (12345, 'a' * 32)),
            ('bot.handlers.TelegramClient', self.client_factory),
        ):
            patcher = patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addAsyncCleanup(h.close_auth_clients)

    def make_client(self, path, *args, **kwargs):
        Path(path).write_bytes(b'new-recipient')
        return self.client

    async def begin(self):
        await h.cb_receiver_login(self.callback, self.state, self.manager)
        self.message.text = '+79991234567'
        await h.process_auth_phone(self.message, self.state)

    async def test_login_promotes_separate_session_and_enables_without_queueing(self):
        await self.begin()
        self.assertEqual(self.recipient.read_bytes(), b'old-recipient')
        self.message.text = '12345'
        await h.process_auth_code(self.message, self.state, self.manager)
        self.assertEqual(self.recipient.read_bytes(), b'new-recipient')
        self.assertEqual(self.recipient.stat().st_mode & 0o777, 0o600)
        self.db.add_session.assert_not_awaited()
        self.db.set_setting.assert_any_await('check_receiver_account_id', '999')
        self.db.set_setting.assert_any_await('check_receiver_enabled', '1')
        self.assertFalse(h.receiver_setup_lock.locked())
        self.assertEqual(self.message.delete.await_count, 2)
        self.assertIsNone(await self.state.get_state())

    async def test_two_factor_password_is_not_trimmed_or_persisted(self):
        await self.begin()
        self.client.sign_in.side_effect = [SessionPasswordNeededError(None), None]
        self.message.text = '12345'
        await h.process_auth_code(self.message, self.state, self.manager)
        self.assertEqual(await self.state.get_state(), AddSessionStates.waiting_for_password.state)
        self.assertEqual(self.recipient.read_bytes(), b'old-recipient')
        self.message.text = ' password with spaces '
        await h.process_auth_password(self.message, self.state, self.manager)
        self.client.sign_in.assert_awaited_with(password=' password with spaces ')
        self.assertEqual(await self.state.get_data(), {})
        self.db.add_session.assert_not_awaited()

    async def test_cancel_keeps_previous_recipient_and_releases_client(self):
        await self.begin()
        await h.cb_receiver_menu(self.callback, self.state)
        self.client.disconnect.assert_awaited()
        self.assertEqual(self.recipient.read_bytes(), b'old-recipient')
        self.assertFalse((self.root / 'pending_123.session').exists())
        self.assertFalse(h.receiver_setup_lock.locked())

    async def test_expired_code_cleans_up_and_does_not_replace_recipient(self):
        await self.begin()
        self.client.sign_in.side_effect = PhoneCodeExpiredError(None)
        self.message.text = '12345'
        await h.process_auth_code(self.message, self.state, self.manager)
        self.assertIsNone(await self.state.get_state())
        self.assertFalse(h.receiver_setup_lock.locked())
        self.assertEqual(self.recipient.read_bytes(), b'old-recipient')
        self.db.set_setting.assert_not_awaited()

    async def test_timeout_cleans_up(self):
        await self.begin()
        with patch('bot.handlers.AUTH_TIMEOUT_SECONDS', 0):
            h.schedule_auth_timeout(123, self.state)
            await asyncio.wait_for(h.auth_timeouts[123], 2)
        self.assertIsNone(await self.state.get_state())
        self.assertFalse(h.receiver_setup_lock.locked())
        self.assertEqual(self.recipient.read_bytes(), b'old-recipient')

    async def test_other_users_and_groups_are_denied(self):
        self.callback.from_user = SimpleNamespace(id=666)
        await h.cb_receiver_login(self.callback, self.state, self.manager)
        self.manager.stop.assert_not_awaited()
        self.callback.from_user = self.user
        self.chat.type = 'group'
        await h.cb_receiver_login(self.callback, self.state, self.manager)
        self.manager.stop.assert_not_awaited()

    async def test_setting_actual_bot_from_link_stops_worker_and_persists(self):
        self.message.text = '@send'
        await h.process_receiver_bot(self.message, self.state, self.manager)
        self.manager.stop.assert_awaited_once()
        self.db.set_setting.assert_awaited_once_with('check_receiver_bot', 'send')

    async def test_disable_overrides_legacy_environment(self):
        await h.cb_receiver_disable(self.callback, self.state, self.manager)
        self.db.set_setting.assert_any_await('check_receiver_enabled', '0')
        self.manager.stop.assert_awaited_once()
