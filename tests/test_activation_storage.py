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
            self.assertEqual(await db.get_setting('sequential_accounts'), '1')
