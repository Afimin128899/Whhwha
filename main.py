import asyncio
import logging
import sys

# Настройка UTF-8 для корректного отображения логов в консоли Windows
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties

from config import BOT_TOKEN, API_POOLS
from database.db import db
from worker.manager import WorkerManager
from bot.handlers import router

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger("main")

async def main():
    if not BOT_TOKEN:
        logger.error("ОШИБКА: Переменная BOT_TOKEN не задана в файле .env! Укажите токен бота.")
        return

    if not API_POOLS:
        logger.warning(
            "ВНИМАНИЕ: API_IDS или API_HASHES не заданы в .env. "
            "Telethon требует их для авторизации сессий!"
        )

    # 1. Инициализация базы данных
    logger.info("Инициализация базы данных SQLite...")
    await db.init()
    await db.reset_processing_sessions()

    import config
    saved_admin_id = await db.get_setting("admin_id", "")
    if saved_admin_id.isdigit():
        config.ADMIN_ID = int(saved_admin_id)
        logger.info(f"Загружен сохраненный Telegram ID администратора: {config.ADMIN_ID}")

    # 2. Инициализация Aiogram Bot & Dispatcher
    bot = DefaultBotProperties(parse_mode=ParseMode.HTML)
    bot_client = Bot(token=BOT_TOKEN, default=bot)
    dp = Dispatcher()

    # 3. Инициализация менеджера воркеров
    worker_mgr = WorkerManager(bot=bot_client)

    # Передаем менеджер воркеров во все хэндлеры
    dp["worker_mgr"] = worker_mgr

    # Подключаем роутер
    dp.include_router(router)

    logger.info("Бот готов к запуску. Запуск polling...")
    try:
        await bot_client.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot_client, worker_mgr=worker_mgr)
    finally:
        logger.info("Остановка приложения...")
        await worker_mgr.stop()
        await bot_client.session.close()
        logger.info("Приложение корректно остановлено.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Программа завершена пользователем.")
