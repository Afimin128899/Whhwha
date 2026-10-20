import io
import sqlite3
import tempfile
import unittest
from pathlib import Path
from zipfile import ZipFile

from unified_bot.diagnostics import (
    MAX_ARCHIVE_SIZE,
    build_audit_report,
    inspect_archive,
    jdjdj_inventory,
    session_project_counts,
    whhwha_project_counts,
)


class SafeIntegrationTests(unittest.TestCase):
    def test_archive_counts_without_extraction(self):
        buf = io.BytesIO()
        with ZipFile(buf, "w") as z:
            z.writestr("a.session", b"test")
            z.writestr("b.json", "{}")
            z.writestr("tdata/map1", "secret-not-read")
            z.writestr("readme.txt", "hello")
        summary = inspect_archive(buf.getvalue())
        self.assertEqual((summary.session_files, summary.metadata_files,
                          summary.tdata_entries, summary.other_files), (1, 1, 1, 1))

    def test_invalid_archive_rejected(self):
        with self.assertRaises(ValueError):
            inspect_archive(b"not a zip")
        with self.assertRaises(ValueError):
            inspect_archive(b"x" * (MAX_ARCHIVE_SIZE + 1))

    def test_missing_databases_are_not_created(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing.sqlite3"
            self.assertIsNone(session_project_counts(missing))
            self.assertIsNone(whhwha_project_counts(missing))
            self.assertFalse(missing.exists())

    def test_legacy_database_adapters_read_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = Path(tmp) / "queue.db"
            with sqlite3.connect(session) as con:
                con.execute("CREATE TABLE queue (status TEXT)")
                con.executemany("INSERT INTO queue VALUES (?)", [("pending",), ("pending",), ("processing",)])
            whhwha = Path(tmp) / "work.db"
            with sqlite3.connect(whhwha) as con:
                con.execute("CREATE TABLE sessions (status TEXT)")
                con.executemany("INSERT INTO sessions VALUES (?)", [("READY",), ("DONE",)])
            self.assertEqual(session_project_counts(session), {"pending": 2, "processing": 1})
            self.assertEqual(whhwha_project_counts(whhwha), {"READY": 1, "DONE": 1})
            self.assertIn("pending: 2", build_audit_report(jdjdj_dir=None,
                                                              session_db=session, whhwha_db=whhwha))

    def test_jdjdj_inventory_only_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "x.session").write_bytes(b"opaque credential")
            (root / "files.zip").write_bytes(b"opaque zip")
            (root / "tdata").mkdir()
            self.assertEqual(jdjdj_inventory(root), {"session": 1, "zip": 1, "tdata_dirs": 1})


if __name__ == "__main__":
    unittest.main()