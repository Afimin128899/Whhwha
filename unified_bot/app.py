"""One aiogram bot for safe cross-project management/diagnostics."""
from __future__ import annotations

import asyncio
from io import BytesIO
import logging
import os
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from dotenv import load_dotenv

from unified_bot.diagnostics import MAX_ARCHIVE_SIZE, build_audit_report, inspect_archive

logger = logging.getLogger(__name__)
router = Router()


def admin_id() -> int:
    raw = os.environ.get("ADMIN_ID", "").strip()
    if not raw.isdecimal() or int(raw) <= 0:
        raise RuntimeError("Укажите числовой ADMIN_ID в окружении.")
    return int(raw)


def is_admin(user_id: int | None) -> bool:
    return user_id is not None and user_id == admin_id()


def paths() -> tuple[Path | None, Path | None, Path | None]:
    def optional_env_path(name: str) -> Path | None:
        value = os.environ.get(name, "").strip()
        return Path(value).expanduser() if value else None

    whhwha = optional_env_path("UNIFIED_WHHWHA_DB")
    if whhwha is None:
        whhwha = Path(__file__).resolve().parent.parent / "bot_database.sqlite3"
    return (optional_env_path("UNIFIED_JDJDJ_DIR"),
            optional_env_path("UNIFIED_SESSION_DB"), whhwha)


def main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Диагностика", callback_data="unified:audit")],
        [InlineKeyboardButton(text="🧩 Источники", callback_data="unified:sources")],
        [InlineKeyboardButton(text="🔐 Безопасность", callback_data="unified:security")],
    ])


WELCOME = (
    "🤖 Единый бот · безопасная панель\n\n"
    "Три источника: Jdjdj, Session и Whhwha.\n"
    "Доступно: диагностика состояния локальных файлов/очередей и просмотр состава ZIP.\n"
    "ZIP можно прислать отдельным документом; содержимое файлов не извлекается.\n\n"
    "Ввод кодов, миграция пользовательских сессий, сброс аккаунтов и автоматические операции с чеками не запускаются."
)

SOURCES = (
    "🧩 Источники\n"
    "• Jdjdj: инвентаризация папки и ZIP-архивов (без чтения сессий).\n"
    "• Session: сводка статусов очереди из SQLite в режиме чтения.\n"
    "• Whhwha: сводка статусов из SQLite в режиме чтения.\n\n"
    "Пути к файлам задаются переменными UNIFIED_JDJDJ_DIR, UNIFIED_SESSION_DB, UNIFIED_WHHWHA_DB."
)

SECURITY = (
    "🔐 Безопасность\n"
    "Доступ только для ADMIN_ID. Не пересылайте .session, tdata или одноразовые коды.\n"
    "Уберите секреты из Git-истории и смените опубликованные BOT_TOKEN/API_HASH.\n"
    "Этот бот никогда не входит в пользовательские аккаунты и не удаляет их данные."
)


@router.message(CommandStart())
@router.message(Command("help"))
async def start(message: Message) -> None:
    if not message.from_user or not is_admin(message.from_user.id):
        await message.answer("Нет доступа.")
        return
    await message.answer(WELCOME, reply_markup=main_keyboard())


@router.message(Command("sources"))
async def cmd_sources(message: Message) -> None:
    if message.from_user and is_admin(message.from_user.id):
        await message.answer(SOURCES, reply_markup=main_keyboard())


@router.message(Command("audit", "status"))
async def cmd_audit(message: Message) -> None:
    if message.from_user and is_admin(message.from_user.id):
        jdjdj_dir, session_db, whhwha_db = paths()
        report = build_audit_report(jdjdj_dir=jdjdj_dir, session_db=session_db, whhwha_db=whhwha_db)
        await message.answer(report, reply_markup=main_keyboard())


@router.callback_query(F.data.startswith("unified:"))
async def callbacks(call: CallbackQuery) -> None:
    if not is_admin(call.from_user.id):
        await call.answer("Нет доступа.", show_alert=True)
        return
    action = call.data or ""
    if action == "unified:audit":
        jdjdj_dir, session_db, whhwha_db = paths()
        text = build_audit_report(jdjdj_dir=jdjdj_dir, session_db=session_db, whhwha_db=whhwha_db)
    elif action == "unified:sources":
        text = SOURCES
    elif action == "unified:security":
        text = SECURITY
    else:
        await call.answer("Неизвестная команда.", show_alert=True)
        return
    if call.message:
        await call.message.answer(text, reply_markup=main_keyboard())
    await call.answer()


@router.message(F.document)
async def inspect_document(message: Message, bot: Bot) -> None:
    if not message.from_user or not is_admin(message.from_user.id):
        await message.answer("Нет доступа.")
        return
    document = message.document
    if not document or not (document.file_name or "").lower().endswith(".zip"):
        await message.answer("Для просмотра состава поддерживаются только ZIP-архивы.")
        return
    if document.file_size and document.file_size > MAX_ARCHIVE_SIZE:
        await message.answer("Архив слишком большой (максимум 20 МиБ).")
        return
    try:
        buffer = BytesIO()
        await bot.download(document, destination=buffer)
        summary = inspect_archive(buffer)
        await message.answer(
            f"📦 ZIP: .session: {summary.session_files}, JSON: {summary.metadata_files}, "
            f"tdata: {summary.tdata_entries}, другие: {summary.other_files}.\n"
            "Внутренние файлы не открывались и не сохранялись."
        )
    except (OSError, ValueError) as exc:
        await message.answer(f"Не удалось проверить архив: {exc}")


async def main() -> None:
    load_dotenv()
    admin_id()  # Refuse to start without a secure administrator ID.
    token = os.environ.get("BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("Укажите BOT_TOKEN в окружении.")
    logging.basicConfig(level=logging.INFO)
    bot = Bot(token)
    dp = Dispatcher()
    dp.include_router(router)
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())