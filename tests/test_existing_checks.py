import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from telethon.errors import FloodWaitError
from worker.telethon_worker import find_existing_checks, process_account_telethon


def message(text='', out=False, buttons=None, entities=None):
    msg = SimpleNamespace(text=text, raw_text=text, out=out, buttons=buttons,
                          entities=entities)
    msg.get_entities_text = lambda: entities or []
    return msg


def history_client(messages, failure=None):
    async def iterate(*args, **kwargs):
        for msg in messages:
            yield msg
        if failure:
            raise failure
    return SimpleNamespace(iter_messages=MagicMock(side_effect=iterate))


class HistoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_full_history_collects_all_links_and_ignores_referrals_and_outgoing(self):
        messages = [message('old text') for _ in range(200)] + [
            message('https://t.me/send?start=one https://t.me/send?start=two'),
            message('https://t.me/redcubebetbot?start=ref'),
            message('https://t.me/otherbot?start=foreign'),
            message('https://t.me/send?start=outgoing', out=True),
            message(buttons=[[SimpleNamespace(url='https://t.me/send?start=button')]]),
            message(entities=[(SimpleNamespace(url='https://t.me/send?start=hidden'), 'Чек')]),
            message('https://t.me/SEND?start=one'),
        ]
        client = history_client(messages)
        self.assertEqual(await find_existing_checks(client, 'redcubebetbot', 'send'), [
            'https://t.me/send?start=one', 'https://t.me/send?start=two',
            'https://t.me/send?start=button', 'https://t.me/send?start=hidden',
        ])
        client.iter_messages.assert_called_once_with('redcubebetbot', limit=None, wait_time=1)

    async def test_partial_scan_error_is_not_treated_as_no_checks(self):
        client = history_client([message('https://t.me/send?start=one')],
                                FloodWaitError(None, capture=30))
        with self.assertRaises(FloodWaitError):
            await find_existing_checks(client, 'redcubebetbot', 'send')


class AccountHistoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        directory = Path(tmp.name)
        (directory / 'one.session').touch()
        self.client = AsyncMock()
        self.client.is_user_authorized.return_value = True
        self.client.get_me.return_value = SimpleNamespace(first_name='Test', username='test', phone='')
        self.client.get_entity.return_value = 'redcubebetbot'
        for target, value in (
            ('worker.telethon_worker.SESSIONS_DIR', directory),
            ('worker.telethon_worker.get_random_api_credentials', lambda: (1, 'hash')),
            ('worker.telethon_worker.TelegramClient', MagicMock(return_value=self.client)),
            ('worker.telethon_worker.db.get_setting', AsyncMock(return_value='send')),
            ('worker.telethon_worker.random_delay', AsyncMock()),
        ):
            p = patch(target, value)
            p.start()
            self.addCleanup(p.stop)

    async def process(self):
        return await process_account_telethon('one', 'https://t.me/redcubebetbot?start=ref', [])

    async def test_existing_checks_do_not_start_bot_or_press_creation_buttons(self):
        links = ['https://t.me/send?start=one', 'https://t.me/send?start=two']
        with patch('worker.telethon_worker.find_existing_checks', AsyncMock(return_value=links)), \
             patch('worker.telethon_worker.click_button_with_alert', AsyncMock()) as click:
            status, details, result = await self.process()
        self.assertEqual((status, result), ('DONE', links))
        self.client.send_message.assert_not_awaited()
        click.assert_not_awaited()
        self.client.disconnect.assert_awaited_once()

    async def test_empty_history_falls_back_to_checks_and_create_buttons(self):
        response = message('menu')
        created = message('https://t.me/send?start=new')
        with patch('worker.telethon_worker.find_existing_checks', AsyncMock(return_value=[])), \
             patch('worker.telethon_worker.wait_for_bot_update', AsyncMock(side_effect=[response, response, response, response, created])), \
             patch('worker.telethon_worker.click_button_with_alert', AsyncMock(side_effect=[(True, None)] * 4)) as click:
            status, details, result = await self.process()
        self.assertEqual((status, result), ('DONE', 'https://t.me/send?start=new'))
        self.assertEqual(click.await_args_list[0].args[1], ['Checks', 'Чеки'])
        self.assertEqual(click.await_args_list[1].args[1], ['Create check', 'Создать чек'])

    async def test_history_failure_prevents_creating_an_extra_check(self):
        with patch('worker.telethon_worker.find_existing_checks', AsyncMock(side_effect=FloodWaitError(None, capture=30))):
            status, details, result = await self.process()
        self.assertEqual(status, 'ERROR')
        self.assertIn('FloodWait', details)
        self.client.send_message.assert_not_awaited()
        self.client.disconnect.assert_awaited_once()
