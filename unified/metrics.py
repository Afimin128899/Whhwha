"""Read-only aggregate metrics for Jdjdj, Session and Whhwha."""
from __future__ import annotations

from contextlib import closing
import os
from pathlib import Path
import sqlite3

QUERIES = {
    "session": {
        "total": "SELECT COUNT(*) FROM queue",
        "pending": "SELECT COUNT(*) FROM queue WHERE status='pending'",
        "processing": "SELECT COUNT(*) FROM queue WHERE status='processing'",
    },
    "whhwha": {
        "total": "SELECT COUNT(*) FROM sessions",
        "ready": "SELECT COUNT(*) FROM sessions WHERE status='READY'",
        "done": "SELECT COUNT(*) FROM sessions WHERE status='DONE'",
        "checks": "SELECT COUNT(*) FROM checks",
    },
}


def read_counts(path: str, module: str) -> dict[str, int] | None:
    """Never create/modify a database or read session contents."""
    if not path:
        return None
    target = Path(path).expanduser()
    if not target.is_file():
        return None
    try:
        uri = target.resolve().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=2)) as conn:
            conn.execute("PRAGMA query_only=ON")
            return {
                key: int(conn.execute(sql).fetchone()[0])
                for key, sql in QUERIES[module].items()
            }
    except (sqlite3.Error, OSError, ValueError):
        return None


def module_status(module: str) -> str:
    """Summarize only counts: no names, auth codes, secrets or payment links."""
    if module == "jdjdj":
        configured = os.getenv("JD_STORAGE_DIR", "").strip()
        if not configured:
            return "Jdjdj: путь к каталогу не настроен."
        path = Path(configured).expanduser()
        if not path.is_dir():
            return "Jdjdj: каталог недоступен."
        try:
            count = sum(1 for f in path.glob("*.session") if f.is_file())
        except OSError:
            return "Jdjdj: каталог недоступен."
        return f"Jdjdj: файлов .session — {count} (только подсчёт)."
    if module == "session":
        counts = read_counts(os.getenv("SESSION_DB_PATH", "").strip(), "session")
        if counts is None:
            return "Session: база данных не настроена или недоступна."
        return (
            f"Session: задач {counts['total']}; ожидают {counts['pending']}; "
            f"обрабатываются {counts['processing']}."
        )
    if module == "whhwha":
        counts = read_counts(os.getenv("WHHWHA_DB_PATH", "").strip(), "whhwha")
        if counts is None:
            return "Whhwha: база данных не настроена или недоступна."
        return (
            f"Whhwha: сессий {counts['total']}; готовы {counts['ready']}; "
            f"завершены {counts['done']}; чеков {counts['checks']}."
        )
    raise ValueError("Unknown module")
