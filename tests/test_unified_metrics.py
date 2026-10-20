import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from unified.metrics import module_status


class MetricsTests(unittest.TestCase):
    def test_unconfigured(self):
        with patch.dict(os.environ, {
            "JD_STORAGE_DIR": "",
            "SESSION_DB_PATH": "",
            "WHHWHA_DB_PATH": "",
        }):
            self.assertIn("не настроен", module_status("jdjdj"))
            self.assertIn("не настроена", module_status("session"))
            self.assertIn("не настроена", module_status("whhwha"))

    def test_counts_only_filenames(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "a.session").write_text("opaque")
            (Path(directory) / "b.txt").write_text("opaque")
            with patch.dict(os.environ, {"JD_STORAGE_DIR": directory}):
                self.assertIn("— 1 (только подсчёт)", module_status("jdjdj"))

    def test_sqlite_read_only_counts(self):
        with tempfile.TemporaryDirectory() as directory:
            session = Path(directory) / "s.db"
            other = Path(directory) / "w.db"
            with sqlite3.connect(session) as db:
                db.execute("CREATE TABLE queue(status TEXT)")
                db.executemany(
                    "INSERT INTO queue VALUES (?)",
                    [("pending",), ("processing",), ("pending",)]
                )
            with sqlite3.connect(other) as db:
                db.execute("CREATE TABLE sessions(status TEXT)")
                db.execute("CREATE TABLE checks(id INTEGER)")
                db.executemany(
                    "INSERT INTO sessions VALUES (?)", [("READY",), ("DONE",)]
                )
                db.execute("INSERT INTO checks VALUES (1)")
            with patch.dict(os.environ, {
                "SESSION_DB_PATH": str(session),
                "WHHWHA_DB_PATH": str(other),
            }):
                self.assertIn("ожидают 2", module_status("session"))
                self.assertIn("чеков 1", module_status("whhwha"))

    def test_unknown_module(self):
        with self.assertRaises(ValueError):
            module_status("unrecognized")


if __name__ == "__main__":
    unittest.main()
