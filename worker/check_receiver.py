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
from database.db import db


RECEIVER_DIR = config.BASE_DIR / "receiver"
RECEIVER_SESSION = RECEIVER_DIR / "recipient.session"
RECEIVER_BOT = "OnoGo_bot"
# Held during interactive login so the worker cannot open the same session file.
receiver_setup_lock = asyncio.Lock()


def parse_check_link(url: str, bot_username: str = RECEIVER_BOT) -> str:
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.netloc.lower() != "t.me"
            or parsed.path.strip("/").lower() != bot_username.lower()
            or parsed.fragment):
        raise ValueError(
            f'Ссылка ведёт не на @{bot_username}. Укажи бота из ссылки '
            'в меню «Аккаунт-получатель» → «Бот из ссылки чека».'
        )
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
        self.bot_username = RECEIVER_BOT

    async def connect(self):
        if not RECEIVER_SESSION.is_file():
            raise RuntimeError("Войди через меню «Аккаунт-получатель» в боте.")
        api_id, api_hash = config.get_random_api_credentials()
        if not api_id or not api_hash:
            raise RuntimeError("Не заданы API_ID/API_HASH.")
        self.client = TelegramClient(
            str(RECEIVER_SESSION), api_id, api_hash,
            lang_code="en", system_lang_code="en-US", flood_sleep_threshold=0,
        )
        await self.client.connect()
        if not await self.client.is_user_authorized():
            raise RuntimeError("Авторизация получателя сброшена. Повтори вход в меню бота.")
        self.bot_username = await db.get_setting('check_receiver_bot', RECEIVER_BOT)
        self.bot = await self.client.get_entity(self.bot_username)
        if not getattr(self.bot, "bot", False):
            raise RuntimeError("Указанный получатель ссылки не является Telegram-ботом.")
        self.ledger = await aiosqlite.connect(RECEIVER_DIR / "opened.sqlite3")
        await self.ledger.execute(
            "CREATE TABLE IF NOT EXISTS opened_checks ("
            "payload TEXT PRIMARY KEY, state TEXT NOT NULL)"
        )
        await self.ledger.commit()

    async def open_check(self, url: str) -> str:
        payload = parse_check_link(url, self.bot_username)
        journal_key = f'{self.bot_username.lower()}:{payload}'
        async with self.lock:
            async with self.ledger.execute(
                "SELECT state FROM opened_checks WHERE payload IN (?, ?)",
                (journal_key, payload if self.bot_username.lower() == RECEIVER_BOT.lower() else journal_key),
            ) as cursor:
                previous = await cursor.fetchone()
            if previous:
                if previous[0] == "SENT":
                    return "Ссылка уже отправлялась получателем."
                return "Результат предыдущей попытки неизвестен. Проверь чек вручную."

            # Commit before the Telegram call: a crash must not cause blind retries.
            await self.ledger.execute(
                "INSERT INTO opened_checks VALUES (?, 'ATTEMPTING')", (journal_key,)
            )
            await self.ledger.commit()
            await self.client(StartBotRequest(
                bot=self.bot, peer=self.bot, start_param=payload,
                random_id=secrets.randbits(63),
            ))
            await self.ledger.execute(
                "UPDATE opened_checks SET state = 'SENT' WHERE payload = ?", (journal_key,)
            )
            await self.ledger.commit()
            return f'Ссылка открыта получателем в @{self.bot_username}. Зачисление не проверялось.'

    async def close(self):
        try:
            if self.client is not None:
                await self.client.disconnect()
        finally:
            if self.ledger is not None:
                await self.ledger.close()
