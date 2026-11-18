"""Authorize your own recipient account locally on the VPS, outside the bot chat."""
import asyncio
import getpass
import os
import warnings

from dotenv import set_key
from telethon import TelegramClient

import config
from database.db import db
from worker.check_receiver import RECEIVER_DIR, RECEIVER_SESSION


def read_secret(prompt: str) -> str:
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        try:
            return getpass.getpass(prompt)
        except getpass.GetPassWarning:
            raise SystemExit("Запусти скрипт в интерактивном терминале для скрытого ввода.") from None


async def main():
    api_id, api_hash = config.get_random_api_credentials()
    if not api_id or not api_hash:
        raise SystemExit("Сначала заполни API_ID/API_HASH через bash install.sh.")
    env_file = config.BASE_DIR / ".env"
    for path in (env_file, RECEIVER_DIR, RECEIVER_SESSION):
        if path.is_symlink():
            raise SystemExit(f"Символическая ссылка не поддерживается: {path.name}")
    os.umask(0o077)
    RECEIVER_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    RECEIVER_DIR.chmod(0o700)
    client = TelegramClient(
        str(RECEIVER_SESSION), api_id, api_hash,
        lang_code="en", system_lang_code="en-US",
    )
    try:
        await client.start(
            phone=lambda: input("Номер аккаунта-получателя с кодом страны: ").strip(),
            code_callback=lambda: read_secret("Код входа из Telegram: ").strip(),
            password=lambda: read_secret("Пароль двухэтапной аутентификации: "),
        )
        me = await client.get_me()
        if me is None or me.bot:
            raise SystemExit("Для получателя нужен пользовательский аккаунт Telegram.")
        print(f"Аккаунт-получатель: Telegram ID {me.id}.")
        if input("Открывать чеки этим аккаунтом? Введи YES: ").strip() != "YES":
            print("Настройки автооткрытия не изменены.")
            return
        set_key(str(env_file), "CHECK_RECEIVER_ENABLED", "1")
        env_file.chmod(0o600)
        await db.init()
        await db.set_setting('check_receiver_account_id', str(me.id))
        await db.set_setting('check_receiver_enabled', '1')
        print("Получатель настроен. Перезапусти службу: sudo systemctl restart whhwha")
    finally:
        await client.disconnect()
        if RECEIVER_SESSION.exists():
            RECEIVER_SESSION.chmod(0o600)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, EOFError):
        print("Вход отменён.")
