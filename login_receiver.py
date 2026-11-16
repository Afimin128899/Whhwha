"""Authorize your own recipient account locally on the VPS, outside the bot chat."""
import asyncio
import getpass
import os

from dotenv import set_key
from telethon import TelegramClient

import config
from database.db import db
from worker.check_receiver import RECEIVER_DIR, RECEIVER_SESSION


async def main():
    api_id, api_hash = config.get_random_api_credentials()
    if not api_id or not api_hash:
        raise SystemExit("Сначала заполни API_ID/API_HASH через bash install.sh.")
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
            code_callback=lambda: getpass.getpass("Код входа из Telegram: ").strip(),
            password=lambda: getpass.getpass("Пароль двухэтапной аутентификации: "),
        )
        me = await client.get_me()
        print(f"Аккаунт-получатель: Telegram ID {me.id}.")
        if input("Открывать чеки этим аккаунтом? Введи YES: ").strip() != "YES":
            print("Автоактивация не включена.")
            return
        env_file = config.BASE_DIR / ".env"
        if env_file.is_symlink():
            raise RuntimeError("Файл .env не должен быть символической ссылкой.")
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
