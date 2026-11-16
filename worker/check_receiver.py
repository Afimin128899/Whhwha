"""Open check deep links using the separately authorized recipient account."""
import asyncio
from pathlib import Path
import re
import secrets
from urllib.parse import parse_qs, urlsplit

import aiosqlite
from telethon import TelegramClient
from telethon.tl.functions.messages import StartBotRequest

import config


RECEIVER_DIR = config.BASE_DIR / "receiver"
RECEIVER_SESSION = RECEIVER_DIR / "recipient.session"
RECEIVER_BOT = "OnoGo_bot"


def parse_check_link(url: str) -> str:
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.netloc.lower() != "t.me"
            or parsed.path.strip("/").lower() != RECEIVER_BOT.lower()
            or parsed.fragment):
        raise ValueError("Ожидалась ссылка чека https://t.me/OnoGo_bot?start=...")
    params = parse_qs(parsed.query)
    payloads = params.get("start", [])
    if (set(params) != {"start"} or len(payloads) != 1
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", payloads[0])):
        raise ValueError("Некорректный start-параметр чека.")
    return payloads[0]


class CheckReceiver:
    def __init__(self):
        self.client = None
        self.ledger = None
        self.lock = asyncio.Lock()
        self.bot = None

    async def connect(self):
        if not RECEIVER_SESSION.is_file():
            raise RuntimeError("Сначала выполни .venv/bin/python login_receiver.py на VPS.")
        api_id, api_hash = config.get_random_api_credentials()
        if not api_id or not api_hash:
            raise RuntimeError("Не заданы API_ID/API_HASH.")
        self.client = TelegramClient(
            str(RECEIVER_SESSION), api_id, api_hash,
            lang_code="en", system_lang_code="en-US", flood_sleep_threshold=0,
        )
        await self.client.connect()
        if not await self.client.is_user_authorized():
            raise RuntimeError("Авторизация получателя сброшена. Повтори login_receiver.py.")
        self.bot = await self.client.get_entity(RECEIVER_BOT)
        if not getattr(self.bot, "bot", False):
            raise RuntimeError("Аккаунт OnoGo_bot не является ботом.")
        self.ledger = await aiosqlite.connect(RECEIVER_DIR / "opened.sqlite3")
        await self.ledger.execute(
            "CREATE TABLE IF NOT EXISTS opened_checks ("
            "payload TEXT PRIMARY KEY, state TEXT NOT NULL)"
        )
        await self.ledger.commit()

    async def open_check(self, url: str) -> str:
        payload = parse_check_link(url)
        async with self.lock:
            async with self.ledger.execute(
                "SELECT state FROM opened_checks WHERE payload = ?", (payload,)
            ) as cursor:
                previous = await cursor.fetchone()
            if previous:
                if previous[0] == "SENT":
                    return "Ссылка уже отправлялась получателем."
                return "Результат предыдущей попытки неизвестен. Проверь чек вручную."

            # Commit before the Telegram call: a crash must not cause blind retries.
            await self.ledger.execute(
                "INSERT INTO opened_checks VALUES (?, 'ATTEMPTING')", (payload,)
            )
            await self.ledger.commit()
            await self.client(StartBotRequest(
                bot=self.bot, peer=self.bot, start_param=payload,
                random_id=secrets.randbits(63),
            ))
            await self.ledger.execute(
                "UPDATE opened_checks SET state = 'SENT' WHERE payload = ?", (payload,)
            )
            await self.ledger.commit()
            return "Ссылка открыта получателем в @OnoGo_bot. Зачисление не проверялось."

    async def close(self):
        try:
            if self.client is not None:
                await self.client.disconnect()
        finally:
            if self.ledger is not None:
                await self.ledger.close()
