import aiosqlite
from datetime import datetime
from typing import Dict, Any, List, Optional
from config import DB_PATH

DEFAULT_SETTINGS = {
    "ref_link": "https://t.me/redcubebetbot?start=CRn6TH6FUBTy",
    "channels": "rc_chat,rcbet",
    "check_amount": "0.1",
    "check_count": "1",
    "delay_min": "2",
    "delay_max": "5",
    "is_running": "0"
}

class Database:
    def __init__(self, db_path=DB_PATH):
        self.db_path = str(db_path)

    async def init(self):
        """Создание таблиц и установка значений по умолчанию."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
            """)

            await db.execute("""
                CREATE TABLE IF NOT EXISTS sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_name TEXT UNIQUE NOT NULL,
                    status TEXT NOT NULL DEFAULT 'READY',
                    details TEXT DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)

            await db.execute("""
                CREATE TABLE IF NOT EXISTS checks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_name TEXT NOT NULL,
                    check_url TEXT NOT NULL,
                    amount REAL NOT NULL,
                    created_at TEXT NOT NULL
                )
            """)

            # Заполняем дефолтные настройки, если их нет
            for key, val in DEFAULT_SETTINGS.items():
                await db.execute(
                    "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                    (key, val)
                )

            await db.commit()

    async def get_setting(self, key: str, default: Optional[str] = None) -> str:
        """Получить значение настройки."""
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("SELECT value FROM settings WHERE key = ?", (key,)) as cursor:
                row = await cursor.fetchone()
                if row:
                    return row[0]
                return default if default is not None else DEFAULT_SETTINGS.get(key, "")

    async def get_all_settings(self) -> Dict[str, str]:
        """Получить все настройки в виде словаря."""
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("SELECT key, value FROM settings") as cursor:
                rows = await cursor.fetchall()
                settings = dict(rows)
                for k, v in DEFAULT_SETTINGS.items():
                    if k not in settings:
                        settings[k] = v
                return settings

    async def set_setting(self, key: str, value: str):
        """Установить значение настройки."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, str(value))
            )
            await db.commit()

    async def add_session(self, session_name: str) -> bool:
        """Добавить сессию в базу."""
        now = datetime.utcnow().isoformat()
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    "INSERT INTO sessions (session_name, status, created_at, updated_at) VALUES (?, 'READY', ?, ?)",
                    (session_name, now, now)
                )
                await db.commit()
                return True
        except aiosqlite.IntegrityError:
            return False

    async def get_next_ready_session(self) -> Optional[str]:
        """Получить следующую сессию для обработки со статусом READY."""
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT session_name FROM sessions WHERE status = 'READY' ORDER BY id ASC LIMIT 1"
            ) as cursor:
                row = await cursor.fetchone()
                if row:
                    return row[0]
                return None

    async def update_session_status(self, session_name: str, status: str, details: str = ""):
        """Обновить статус обработки сессии."""
        now = datetime.utcnow().isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE sessions SET status = ?, details = ?, updated_at = ? WHERE session_name = ?",
                (status, details, now, session_name)
            )
            await db.commit()

    async def reset_processing_sessions(self):
        """Сбросить статус зависших сессий (PROCESSING -> READY)."""
        now = datetime.utcnow().isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE sessions SET status = 'READY', updated_at = ? WHERE status = 'PROCESSING'",
                (now,)
            )
            await db.commit()

    async def reset_all_sessions(self):
        """Сбросить все сессии в статус READY для повторного прогона."""
        now = datetime.utcnow().isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE sessions SET status = 'READY', details = '', updated_at = ?",
                (now,)
            )
            await db.commit()

    async def get_sessions_stats(self) -> Dict[str, int]:
        """Получить статистику по сессиям."""
        async with aiosqlite.connect(self.db_path) as db:
            stats = {
                "TOTAL": 0,
                "READY": 0,
                "PROCESSING": 0,
                "DONE": 0,
                "ERROR": 0,
                "INVALID": 0
            }
            async with db.execute("SELECT status, COUNT(*) FROM sessions GROUP BY status") as cursor:
                rows = await cursor.fetchall()
                for status, count in rows:
                    if status in stats:
                        stats[status] = count
                    stats["TOTAL"] += count
            return stats

    async def add_check(self, session_name: str, check_url: str, amount: float):
        """Сохранить полученный чек в базу."""
        now = datetime.utcnow().isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO checks (session_name, check_url, amount, created_at) VALUES (?, ?, ?, ?)",
                (session_name, check_url, amount, now)
            )
            await db.commit()

    async def get_all_checks(self) -> List[Dict[str, Any]]:
        """Получить все сохраненные чеки."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM checks ORDER BY id DESC") as cursor:
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]

    async def get_checks_stats(self) -> Dict[str, Any]:
        """Статистика по чекам."""
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("SELECT COUNT(*), COALESCE(SUM(amount), 0.0) FROM checks") as cursor:
                count, total_amount = await cursor.fetchone()
                return {"count": count, "total_amount": round(total_amount, 2)}

db = Database()
