"""Single-token Telegram dashboard (does not access login credentials)."""
from __future__ import annotations

import asyncio
import logging
import os

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from dotenv import load_dotenv
from unified.metrics import module_status

load_dotenv()
router = Router()
ADMINS: set[int] = set()
MODULES = {"jdjdj": "Jdjdj", "session": "Session", "whhwha": "Whhwha"}

WELCOME = (
    "<b>Единая панель</b>\n\n"
    "Один бот для просмотра статистики Jdjdj, Session и Whhwha.\n"
    "Только чтение: коды входа, файлы авторизации, миграция аккаунтов "
    "и массовая автоматизация не обрабатываются."
)


def admins_from_environment() -> set[int]:
    raw = os.getenv("UNIFIED_ADMIN_IDS", "")
    try:
        parsed = {int(i.strip()) for i in raw.split(",") if i.strip()}
    except ValueError as exc:
        raise RuntimeError("UNIFIED_ADMIN_IDS: укажите числовые ID") from exc
    if not parsed or any(value <= 0 for value in parsed):
        raise RuntimeError("Укажите UNIFIED_ADMIN_IDS перед запуском")
    return parsed


def allowed(user_id: int | None) -> bool:
    return user_id is not None and user_id in ADMINS


def menu() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=label, callback_data="module:" + key)]
        for key, label in MODULES.items()
    ]
    rows.append([InlineKeyboardButton(text="📊 Сводка", callback_data="summary")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def back() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="home")]
    ])


async def report() -> str:
    rows = await asyncio.gather(
        *(asyncio.to_thread(module_status, key) for key in MODULES)
    )
    return "<b>Сводка трёх проектов</b>\n\n" + "\n".join(rows)


@router.message(CommandStart())
@router.message(Command("menu"))
async def command_menu(message: Message) -> None:
    if not allowed(message.from_user.id if message.from_user else None):
        await message.answer("Доступ запрещён.")
        return
    await message.answer(WELCOME, reply_markup=menu())


@router.message(Command("status"))
async def command_status(message: Message) -> None:
    if not allowed(message.from_user.id if message.from_user else None):
        await message.answer("Доступ запрещён.")
        return
    await message.answer(await report(), reply_markup=back())


@router.message(Command("help"))
async def command_help(message: Message) -> None:
    if not allowed(message.from_user.id if message.from_user else None):
        await message.answer("Доступ запрещён.")
        return
    await message.answer(
        "Команды: /start, /menu, /status, /help.\n"
        "Файлы Telegram-сессий и коды авторизации сюда не присылайте.",
        reply_markup=menu(),
    )


@router.callback_query(F.data == "home")
async def callback_home(callback: CallbackQuery) -> None:
    if not allowed(callback.from_user.id):
        await callback.answer("Доступ запрещён.", show_alert=True)
        return
    await callback.answer()
    if callback.message:
        await callback.message.edit_text(WELCOME, reply_markup=menu())


@router.callback_query(F.data == "summary")
async def callback_summary(callback: CallbackQuery) -> None:
    if not allowed(callback.from_user.id):
        await callback.answer("Доступ запрещён.", show_alert=True)
        return
    await callback.answer()
    summary = await report()
    if callback.message:
        await callback.message.edit_text(summary, reply_markup=back())


@router.callback_query(F.data.startswith("module:"))
async def callback_module(callback: CallbackQuery) -> None:
    if not allowed(callback.from_user.id):
        await callback.answer("Доступ запрещён.", show_alert=True)
        return
    module = (callback.data or "").removeprefix("module:")
    if module not in MODULES:
        await callback.answer("Неизвестный раздел.", show_alert=True)
        return
    await callback.answer()
    summary = await asyncio.to_thread(module_status, module)
    if callback.message:
        await callback.message.edit_text(
            f"<b>{MODULES[module]}</b>\n\n{summary}", reply_markup=back()
        )


@router.message(F.document)
async def reject_files(message: Message) -> None:
    if not allowed(message.from_user.id if message.from_user else None):
        await message.answer("Доступ запрещён.")
        return
    await message.answer(
        "Документы не скачиваются и не сохраняются. "
        "Не отправляйте .session, tdata, архивы аккаунтов или коды входа."
    )


async def main() -> None:
    global ADMINS
    ADMINS = admins_from_environment()
    token = os.getenv("UNIFIED_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("Укажите UNIFIED_BOT_TOKEN")
    bot = Bot(token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
