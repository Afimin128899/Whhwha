import unittest
from types import SimpleNamespace

from worker.telethon_worker import extract_check_url


class CheckExtractionTests(unittest.TestCase):
    def message(self, text='', entities=None, buttons=None):
        return SimpleNamespace(text=text, raw_text=text, entities=entities, buttons=buttons,
                               get_entities_text=lambda: entities or [])

    def test_plain_check_link(self):
        self.assertEqual(extract_check_url(self.message('Check: https://t.me/send?start=ABC')),
                         'https://t.me/send?start=ABC')

    def test_hyperlinked_label(self):
        msg = self.message('Open check', entities=[
            (SimpleNamespace(url='https://t.me/send?start=ABC'), 'Open check')])
        self.assertEqual(extract_check_url(msg), 'https://t.me/send?start=ABC')

    def test_inline_url_button(self):
        msg = self.message('Created', buttons=[[
            SimpleNamespace(url='https://t.me/send?start=ABC')]])
        self.assertEqual(extract_check_url(msg), 'https://t.me/send?start=ABC')

    def test_unrelated_button_is_not_a_check(self):
        msg = self.message('Menu', buttons=[[
            SimpleNamespace(url='https://example.org'), SimpleNamespace(url=None)]])
        self.assertIsNone(extract_check_url(msg))
