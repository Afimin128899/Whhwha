"""Open check deep links using the separately authorized recipient account."""
import asyncio
from pathlib import Path
import time
import re
import secrets
from html import escape
from urllib.parse import parse_qs, urlsplit

import aiosqlite
from telethon import TelegramClient
from telethon.errors import FloodWaitError
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


ACTIVATION_LABELS = {
    'ACTIVATED': '✅ Чек активирован: бот подтвердил получение.',
    'ALREADY_ACTIVATED': 'ℹ️ Чек уже активирован ранее (ответ бота).',
    'REJECTED': '❌ Чек не активирован: бот отклонил активацию.',
    'UNKNOWN': '❓ Активация чека не подтверждена. Проверь вручную.',
}


def is_receipt_confirmation(text: str) -> bool:
    return bool(re.fullmatch(
        r'вы получили \d+(?:[.,]\d+)?\s*\$\.?', ' '.join(text.lower().split())
    ))


def classify_activation(text: str) -> str:
    """Recognize explicit results; generic menus and balances prove nothing."""
    text = ' '.join(text.lower().split())
    if re.search(r'чек (?:уже активирован|уже был активирован)|check (?:has )?(?:already been activated|already redeemed)|you have already (?:activated|redeemed)', text):
        return 'ALREADY_ACTIVATED'
    if re.search(r'чек (?:не найден|недействителен|ист[её]к|удал[её]н)|не удалось активировать|check (?:is )?(?:expired|invalid|not found)|cannot (?:activate|redeem)', text):
        return 'REJECTED'
    if is_receipt_confirmation(text):
        return 'ACTIVATED'
    if re.search(r'(?:вы|ты) (?:успешно )?активировал[аи]? чек|чек успешно активирован|you (?:have )?(?:successfully )?(?:received|redeemed) \d|you (?:have )?successfully activated (?:the |a )?check', text):
        return 'ACTIVATED'
    return 'UNKNOWN'


class CheckReceiver:
    def __init__(self):
        self.client = None
        self.ledger = None
        self.lock = asyncio.Lock()
        self.bot = None
        self.bot_username = RECEIVER_BOT
        self._next_attempt = 0.0
        self._clock = time.monotonic
        self._sleep = asyncio.sleep

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
            if previous and previous[0] != "WAITING":
                if previous[0] in ACTIVATION_LABELS:
                    return ACTIVATION_LABELS[previous[0]]
                if previous[0] == "SENT":
                    return "Ссылка уже отправлялась получателем."
                return "Результат предыдущей попытки неизвестен. Проверь чек вручную."

            # Commit before the Telegram call: a crash must not cause blind retries.
            await self.ledger.execute(
                "INSERT OR REPLACE INTO opened_checks VALUES (?, 'WAITING')", (journal_key,)
            )
            await self.ledger.commit()
            request = StartBotRequest(
                bot=self.bot, peer=self.bot, start_param=payload,
                random_id=secrets.randbits(63),
            )
            while True:
                remaining = self._next_attempt - self._clock()
                if remaining > 0:
                    await self._sleep(remaining)
                await self.ledger.execute(
                    "UPDATE opened_checks SET state = 'ATTEMPTING' WHERE payload = ?",
                    (journal_key,),
                )
                await self.ledger.commit()
                # Reserve the interval even if the request fails or is cancelled.
                self._next_attempt = self._clock() + 10.0
                try:
                    latest = await self.client.get_messages(self.bot, limit=1)
                    baseline = latest[0].id if latest else 0
                    updates = await self.client(request)
                    break
                except FloodWaitError as exc:
                    # Telegram rejected the request: retry only after its wait expires.
                    self._next_attempt = max(self._next_attempt, self._clock() + exc.seconds + 1)
                    await self.ledger.execute(
                        "UPDATE opened_checks SET state = 'WAITING' WHERE payload = ?",
                        (journal_key,),
                    )
                    await self.ledger.commit()
            await self.ledger.execute(
                "UPDATE opened_checks SET state = 'SENT' WHERE payload = ?", (journal_key,)
            )
            await self.ledger.commit()
            state, note = await self._observe_activation(payload, baseline, updates)
            await self.ledger.execute(
                "UPDATE opened_checks SET state = ? WHERE payload = ?", (state, journal_key)
            )
            await self.ledger.commit()
            return note

    async def _observe_activation(self, payload, baseline, updates):
        outgoing_ids = {
            message.id for update in getattr(updates, 'updates', [])
            if (message := getattr(update, 'message', None)) is not None
            and getattr(message, 'out', False)
        }
        boundary = max(outgoing_ids, default=baseline)
        last_text = ''
        try:
            async with asyncio.timeout(8):
                while True:
                    messages = await self.client.get_messages(self.bot, limit=10)
                    for message in reversed(messages):
                        if message.out or message.id <= boundary:
                            continue
                        text = message.raw_text or ''
                        last_text = text[:500]
                        reply_to = getattr(message, 'reply_to_msg_id', None)
                        correlated = reply_to in outgoing_ids or bool(re.search(
                            r'(?<![A-Za-z0-9_-])' + re.escape(payload) + r'(?![A-Za-z0-9_-])', text))
                        state = classify_activation(text)
                        # Exact receipt text supplied by the owner is also accepted
                        # as the fresh reply in this serialized bot dialogue.
                        if (correlated or is_receipt_confirmation(text)) and state != 'UNKNOWN':
                            return state, ACTIVATION_LABELS[state] + '\nОтвет бота: ' + escape(last_text)
                    await asyncio.sleep(1)
        except TimeoutError:
            pass
        except Exception:
            # A read failure after sending must never trigger another activation.
            pass
        note = ACTIVATION_LABELS['UNKNOWN']
        if last_text:
            note += '\nОтвет бота (связь с этим чеком не подтверждена): ' + escape(last_text)
        return 'UNKNOWN', note

    async def close(self):
        try:
            if self.client is not None:
                await self.client.disconnect()
        finally:
            if self.ledger is not None:
                await self.ledger.close()
