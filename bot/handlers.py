from __future__ import annotations
import os
import io
import re
from pathlib import Path
from aiogram import Router, F, types, Bot
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile
from aiogram.enums import ParseMode

from telethon import TelegramClient
from telethon.errors import (
    SessionPasswordNeededError,
    PhoneCodeInvalidError,
    PhoneCodeExpiredError,
    PasswordHashInvalidError,
    FloodWaitError
)

import config
from config import SESSIONS_DIR, get_random_api_credentials
from database.db import db
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from worker.manager import WorkerManager
from bot.keyboards import (
    get_main_keyboard,
    get_settings_keyboard,
    get_back_cancel_keyboard,
    get_reset_confirm_keyboard
)
from bot.states import SettingsStates, AddSessionStates
from bot.emojis import tg_emoji, emoji_id

router = Router()

# Временное хранилище Telethon клиентов в процессе авторизации (user_id -> TelegramClient)
active_auth_clients: dict[int, TelegramClient] = {}

async def check_admin(user: types.User) -> bool:
    """Проверка прав администратора по ID или username (@Iolzi)."""
    if not user:
        return False

    # 1. Если совпал числовой ID
    if config.ADMIN_ID and user.id == config.ADMIN_ID:
        return True

    # 2. Если совпал username
    username = (user.username or "").lower().strip()
    if config.ADMIN_USERNAME and username == config.ADMIN_USERNAME:
        config.ADMIN_ID = user.id
        await db.set_setting("admin_id", str(user.id))
        return True

    # 3. Проверка сохраненного ID в базе
    saved_id = await db.get_setting("admin_id", "")
    if saved_id.isdigit() and user.id == int(saved_id):
        config.ADMIN_ID = user.id
        return True

    return False

@router.message(CommandStart())
@router.message(Command("menu"))
async def cmd_start(message: types.Message, state: FSMContext, worker_mgr: WorkerManager):
    """Точка входа /start."""
    if not await check_admin(message.from_user):
        await message.answer(
            f"{tg_emoji('forbidden')} У вас нет доступа к этой панели управления.",
            parse_mode=ParseMode.HTML
        )
        return

    await state.clear()
    is_running = worker_mgr.is_running()
    status_text = (
        f"{tg_emoji('check')} <b>Работает</b>"
        if is_running
        else f"{tg_emoji('cross')} <b>Остановлен</b>"
    )
    text = (
        f"<b>{tg_emoji('robot')} Панель управления фермой Telethon</b>\n\n"
        f"{tg_emoji('lightning')} <b>Статус воркера:</b> {status_text}\n\n"
        f"{tg_emoji('idea')} <i>Вы можете отправлять файлы .session прямо в этот чат — они автоматически добавятся в очередь.</i>"
    )
    await message.answer(text, reply_markup=get_main_keyboard(is_running), parse_mode=ParseMode.HTML)

@router.callback_query(F.data == "menu_main")
async def cb_menu_main(callback: types.CallbackQuery, state: FSMContext | None = None, worker_mgr: WorkerManager | None = None):
    """Возврат в главное меню."""
    if not await check_admin(callback.from_user):
        return await callback.answer("Нет доступа", show_alert=True)

    if state is not None:
        await state.clear()

    is_running = worker_mgr.is_running() if worker_mgr else False
    status_text = (
        f"{tg_emoji('check')} <b>Работает</b>"
        if is_running
        else f"{tg_emoji('cross')} <b>Остановлен</b>"
    )
    text = (
        f"<b>{tg_emoji('robot')} Панель управления фермой Telethon</b>\n\n"
        f"{tg_emoji('lightning')} <b>Статус воркера:</b> {status_text}\n\n"
        f"{tg_emoji('idea')} <i>Вы можете отправлять файлы .session прямо в этот чат — они автоматически добавятся в очередь.</i>"
    )
    try:
        await callback.message.edit_text(text, reply_markup=get_main_keyboard(is_running), parse_mode=ParseMode.HTML)
    except Exception:
        await callback.message.answer(text, reply_markup=get_main_keyboard(is_running), parse_mode=ParseMode.HTML)
    await callback.answer()

@router.callback_query(F.data == "worker_start")
async def cb_worker_start(callback: types.CallbackQuery, state: FSMContext, worker_mgr: WorkerManager):
    """Запуск воркера."""
    if not await check_admin(callback.from_user):
        return await callback.answer("Нет доступа", show_alert=True)

    stats = await db.get_sessions_stats()
    if stats.get("READY", 0) == 0:
        return await callback.answer(
            "⚠️ Нет сессий со статусом READY!\nСначала закиньте сессии или выполните сброс.",
            show_alert=True
        )

    started = await worker_mgr.start()
    if started:
        await callback.answer("Воркер успешно запущен!", show_alert=False)
    else:
        await callback.answer("Воркер уже работает.", show_alert=False)

    await cb_menu_main(callback, state, worker_mgr)

@router.callback_query(F.data == "worker_stop")
async def cb_worker_stop(callback: types.CallbackQuery, state: FSMContext, worker_mgr: WorkerManager):
    """Остановка воркера."""
    if not await check_admin(callback.from_user):
        return await callback.answer("Нет доступа", show_alert=True)

    await worker_mgr.stop()
    await callback.answer("Воркер остановлен.", show_alert=False)
    await cb_menu_main(callback, state, worker_mgr)

@router.callback_query(F.data == "view_stats")
async def cb_view_stats(callback: types.CallbackQuery):
    """Просмотр статистики."""
    if not await check_admin(callback.from_user):
        return await callback.answer("Нет доступа", show_alert=True)

    s_stats = await db.get_sessions_stats()
    c_stats = await db.get_checks_stats()

    text = (
        f"{tg_emoji('stats')} <b>Статистика работы системы:</b>\n\n"
        f"{tg_emoji('folder')} <b>Всего сессий в базе:</b> {s_stats['TOTAL']}\n"
        f"  {tg_emoji('hourglass')} Ожидают (READY): <b>{s_stats['READY']}</b>\n"
        f"  {tg_emoji('settings')} В процессе: <b>{s_stats['PROCESSING']}</b>\n"
        f"  {tg_emoji('check')} Успешно (DONE): <b>{s_stats['DONE']}</b>\n"
        f"  {tg_emoji('cross')} Ошибок (ERROR): <b>{s_stats['ERROR']}</b>\n"
        f"  {tg_emoji('forbidden')} Невалидных сессий: <b>{s_stats['INVALID']}</b>\n\n"
        f"{tg_emoji('dollar')} <b>Собрано чеков:</b> <b>{c_stats['count']}</b> шт.\n"
        f"{tg_emoji('money')} <b>Общая сумма чеков:</b> <b>{c_stats['total_amount']}$</b>"
    )
    kb = types.InlineKeyboardMarkup(inline_keyboard=[
        [
            types.InlineKeyboardButton(
                text="Назад",
                callback_data="menu_main",
                icon_custom_emoji_id=emoji_id("back")
            )
        ]
    ])
    await callback.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    await callback.answer()

@router.callback_query(F.data == "view_checks")
async def cb_view_checks(callback: types.CallbackQuery):
    """Просмотр последних чеков."""
    if not await check_admin(callback.from_user):
        return await callback.answer("Нет доступа", show_alert=True)

    checks = await db.get_all_checks()
    if not checks:
        return await callback.answer("Чеков пока нет.", show_alert=True)

    last_checks = checks[:15]
    lines = [f"{tg_emoji('dollar')} <b>Последние полученные чеки:</b>\n"]
    for i, c in enumerate(last_checks, 1):
        lines.append(f"{i}. <code>{c['session_name']}</code> | {c['amount']}$ -> {c['check_url']}")

    if len(checks) > 15:
        lines.append(f"\n<i>... и еще {len(checks) - 15} чеков. Нажмите кнопку выгрузки для полного списка.</i>")

    kb = types.InlineKeyboardMarkup(inline_keyboard=[
        [
            types.InlineKeyboardButton(
                text="Скачать все .txt",
                callback_data="export_checks_file",
                icon_custom_emoji_id=emoji_id("download")
            )
        ],
        [
            types.InlineKeyboardButton(
                text="Назад",
                callback_data="menu_main",
                icon_custom_emoji_id=emoji_id("back")
            )
        ]
    ])
    await callback.message.edit_text(
        "\n".join(lines),
        reply_markup=kb,
        disable_web_page_preview=True,
        parse_mode=ParseMode.HTML
    )
    await callback.answer()

@router.callback_query(F.data == "export_checks_file")
async def cb_export_checks(callback: types.CallbackQuery):
    """Выгрузка всех чеков файлом."""
    if not await check_admin(callback.from_user):
        return await callback.answer("Нет доступа", show_alert=True)

    checks = await db.get_all_checks()
    if not checks:
        return await callback.answer("Нет чеков для выгрузки.", show_alert=True)

    content = "\n".join([c["check_url"] for c in checks])
    file_bytes = content.encode("utf-8")
    input_file = BufferedInputFile(file_bytes, filename="checks.txt")

    await callback.message.answer_document(
        input_file,
        caption=f"{tg_emoji('check')} Всего выгружено: <b>{len(checks)}</b> чеков.",
        parse_mode=ParseMode.HTML
    )
    await callback.answer()

@router.callback_query(F.data == "scan_sessions")
async def cb_scan_sessions(callback: types.CallbackQuery, state: FSMContext, worker_mgr: WorkerManager):
    """Сканирование папки sessions на наличие новых файлов."""
    if not await check_admin(callback.from_user):
        return await callback.answer("Нет доступа", show_alert=True)

    files = list(SESSIONS_DIR.glob("*.session"))
    added = 0
    for f in files:
        session_name = f.stem
        if await db.add_session(session_name):
            added += 1

    await callback.answer(
        f"Найдено файлов: {len(files)}\nДобавлено новых в очередь: {added}",
        show_alert=True
    )
    await cb_menu_main(callback, state, worker_mgr)

@router.callback_query(F.data == "reset_sessions_confirm")
async def cb_reset_sessions_confirm(callback: types.CallbackQuery):
    """Запрос подтверждения сброса."""
    if not await check_admin(callback.from_user):
        return await callback.answer("Нет доступа", show_alert=True)

    text = (
        f"{tg_emoji('warning')} <b>Вы уверены, что хотите сбросить ВСЕ сессии в статус READY?</b>\n\n"
        "Они будут обработаны заново при следующем запуске воркера."
    )
    await callback.message.edit_text(text, reply_markup=get_reset_confirm_keyboard(), parse_mode=ParseMode.HTML)
    await callback.answer()

@router.callback_query(F.data == "reset_sessions_do")
async def cb_reset_sessions_do(callback: types.CallbackQuery, state: FSMContext, worker_mgr: WorkerManager):
    """Выполнение сброса."""
    if not await check_admin(callback.from_user):
        return await callback.answer("Нет доступа", show_alert=True)

    await db.reset_all_sessions()
    await callback.answer("Все сессии переведены в статус READY!", show_alert=True)
    await cb_menu_main(callback, state, worker_mgr)

# --- МЕНЮ НАСТРОЕК ---

@router.callback_query(F.data == "menu_settings")
async def cb_menu_settings(callback: types.CallbackQuery, state: FSMContext):
    """Отображение настроек."""
    if not await check_admin(callback.from_user):
        return await callback.answer("Нет доступа", show_alert=True)

    await state.clear()
    settings = await db.get_all_settings()
    text = (
        f"{tg_emoji('settings')} <b>Текущие настройки системы:</b>\n\n"
        f"{tg_emoji('link')} <b>Реферальная ссылка:</b>\n<code>{settings.get('ref_link', '')}</code>\n\n"
        f"{tg_emoji('speaker')} <b>Каналы для подписки:</b> <code>{settings.get('channels', '')}</code>\n"
        f"{tg_emoji('dollar')} <b>Сумма чека:</b> <code>{settings.get('check_amount', '0.1')}$</code>\n"
        f"{tg_emoji('stopwatch')} <b>Интервал пауз:</b> <code>{settings.get('delay_min', '2')} - {settings.get('delay_max', '5')} сек</code>\n\n"
        "<i>Нажмите на кнопку ниже, чтобы изменить соответствующий параметр.</i>"
    )
    await callback.message.edit_text(text, reply_markup=get_settings_keyboard(settings), parse_mode=ParseMode.HTML)
    await callback.answer()

@router.callback_query(F.data == "set_ref_link")
async def cb_set_ref_link(callback: types.CallbackQuery, state: FSMContext):
    if not await check_admin(callback.from_user):
        return
    await state.set_state(SettingsStates.waiting_for_ref_link)
    text = (
        f"{tg_emoji('link')} <b>Отправьте новую реферальную ссылку:</b>\n"
        "(Например: <code>https://t.me/redcubebetbot?start=CRn6TH6FUBTy</code>)"
    )
    await callback.message.edit_text(text, reply_markup=get_back_cancel_keyboard(), parse_mode=ParseMode.HTML)
    await callback.answer()

@router.message(SettingsStates.waiting_for_ref_link)
async def process_new_ref_link(message: types.Message, state: FSMContext):
    if not await check_admin(message.from_user):
        return
    new_link = message.text.strip()
    if not ("t.me/" in new_link or "start=" in new_link):
        await message.answer(
            f"{tg_emoji('warning')} Некорректная ссылка! Попробуйте снова или нажмите 'Отмена'.",
            parse_mode=ParseMode.HTML
        )
        return
    await db.set_setting("ref_link", new_link)
    await state.clear()
    await message.answer(
        f"{tg_emoji('check')} Реферальная ссылка обновлена на:\n<code>{new_link}</code>",
        parse_mode=ParseMode.HTML
    )
    settings = await db.get_all_settings()
    await message.answer(
        f"{tg_emoji('settings')} Меню настроек:",
        reply_markup=get_settings_keyboard(settings),
        parse_mode=ParseMode.HTML
    )

@router.callback_query(F.data == "set_channels")
async def cb_set_channels(callback: types.CallbackQuery, state: FSMContext):
    if not await check_admin(callback.from_user):
        return
    await state.set_state(SettingsStates.waiting_for_channels)
    text = (
        f"{tg_emoji('speaker')} <b>Отправьте список каналов/чатов через запятую:</b>\n"
        "(Например: <code>rc_chat, rcbet</code>)"
    )
    await callback.message.edit_text(text, reply_markup=get_back_cancel_keyboard(), parse_mode=ParseMode.HTML)
    await callback.answer()

@router.message(SettingsStates.waiting_for_channels)
async def process_new_channels(message: types.Message, state: FSMContext):
    if not await check_admin(message.from_user):
        return
    channels = message.text.strip()
    await db.set_setting("channels", channels)
    await state.clear()
    await message.answer(
        f"{tg_emoji('check')} Каналы обновлены: <code>{channels}</code>",
        parse_mode=ParseMode.HTML
    )
    settings = await db.get_all_settings()
    await message.answer(
        f"{tg_emoji('settings')} Меню настроек:",
        reply_markup=get_settings_keyboard(settings),
        parse_mode=ParseMode.HTML
    )

@router.callback_query(F.data == "set_check_amount")
async def cb_set_check_amount(callback: types.CallbackQuery, state: FSMContext):
    if not await check_admin(callback.from_user):
        return
    await state.set_state(SettingsStates.waiting_for_check_amount)
    text = (
        f"{tg_emoji('dollar')} <b>Отправьте сумму чека (USD):</b>\n"
        "(Например: <code>0.1</code>)"
    )
    await callback.message.edit_text(text, reply_markup=get_back_cancel_keyboard(), parse_mode=ParseMode.HTML)
    await callback.answer()

@router.message(SettingsStates.waiting_for_check_amount)
async def process_new_amount(message: types.Message, state: FSMContext):
    if not await check_admin(message.from_user):
        return
    val = message.text.strip().replace(",", ".")
    try:
        float(val)
    except ValueError:
        await message.answer(
            f"{tg_emoji('warning')} Введите число (например, <code>0.1</code>):",
            parse_mode=ParseMode.HTML
        )
        return
    await db.set_setting("check_amount", val)
    await state.clear()
    await message.answer(
        f"{tg_emoji('check')} Сумма чека обновлена: <code>{val}$</code>",
        parse_mode=ParseMode.HTML
    )
    settings = await db.get_all_settings()
    await message.answer(
        f"{tg_emoji('settings')} Меню настроек:",
        reply_markup=get_settings_keyboard(settings),
        parse_mode=ParseMode.HTML
    )

@router.callback_query(F.data == "set_delays")
async def cb_set_delays(callback: types.CallbackQuery, state: FSMContext):
    if not await check_admin(callback.from_user):
        return
    await state.set_state(SettingsStates.waiting_for_delays)
    text = (
        f"{tg_emoji('stopwatch')} <b>Отправьте минимальную и максимальную паузу в секундах через дефис:</b>\n"
        "(Например: <code>2-5</code>)"
    )
    await callback.message.edit_text(text, reply_markup=get_back_cancel_keyboard(), parse_mode=ParseMode.HTML)
    await callback.answer()

@router.message(SettingsStates.waiting_for_delays)
async def process_new_delays(message: types.Message, state: FSMContext):
    if not await check_admin(message.from_user):
        return
    val = message.text.strip()
    match = re.match(r"^(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)$", val)
    if not match:
        await message.answer(
            f"{tg_emoji('warning')} Неверный формат! Введите, например: <code>2-5</code>",
            parse_mode=ParseMode.HTML
        )
        return
    d_min, d_max = match.group(1), match.group(2)
    await db.set_setting("delay_min", d_min)
    await db.set_setting("delay_max", d_max)
    await state.clear()
    await message.answer(
        f"{tg_emoji('check')} Задержки обновлены: <code>{d_min} - {d_max} сек</code>",
        parse_mode=ParseMode.HTML
    )
    settings = await db.get_all_settings()
    await message.answer(
        f"{tg_emoji('settings')} Меню настроек:",
        reply_markup=get_settings_keyboard(settings),
        parse_mode=ParseMode.HTML
    )

# --- ПРИЕМ ФАЙЛОВ СЕССИЙ (.session) ---

@router.message(F.document)
async def handle_session_file(message: types.Message, bot: Bot):
    """Автоматическое сохранение сессии при отправке документа админом."""
    if not await check_admin(message.from_user):
        return

    doc = message.document
    if not doc.file_name.endswith(".session"):
        await message.answer(
            f"{tg_emoji('warning')} Принимаются только файлы с расширением <code>.session</code>.",
            parse_mode=ParseMode.HTML
        )
        return

    dest = SESSIONS_DIR / doc.file_name
    await bot.download(doc, destination=dest)
    session_name = Path(doc.file_name).stem
    added = await db.add_session(session_name)

    if added:
        await message.answer(
            f"{tg_emoji('check')} Сессия <code>{doc.file_name}</code> сохранена и добавлена в очередь!",
            parse_mode=ParseMode.HTML
        )
    else:
        await message.answer(
            f"{tg_emoji('info')} Сессия <code>{doc.file_name}</code> уже была в базе данных.",
            parse_mode=ParseMode.HTML
        )

# --- АВТОРИЗАЦИЯ ПО НОМЕРУ И КОДУ ---

@router.callback_query(F.data == "add_session_phone")
async def cb_add_session_phone(callback: types.CallbackQuery, state: FSMContext):
    """Начало процесса входа по номеру телефона."""
    if not await check_admin(callback.from_user):
        return await callback.answer("Нет доступа", show_alert=True)

    # Закрываем предыдущую незавершенную попытку, если была
    old_client = active_auth_clients.pop(callback.from_user.id, None)
    if old_client:
        try:
            await old_client.disconnect()
        except Exception:
            pass

    await state.set_state(AddSessionStates.waiting_for_phone)
    text = (
        f"{tg_emoji('phone')} <b>Вход в аккаунт по номеру телефона</b>\n\n"
        "Отправьте номер телефона в международном формате:\n"
        "Например: <code>+79991234567</code> или <code>79991234567</code>"
    )
    await callback.message.edit_text(
        text,
        reply_markup=get_back_cancel_keyboard(to_where="menu_main"),
        parse_mode=ParseMode.HTML
    )
    await callback.answer()

@router.message(AddSessionStates.waiting_for_phone)
async def process_auth_phone(message: types.Message, state: FSMContext):
    """Обработка ввода номера телефона."""
    if not await check_admin(message.from_user):
        return

    raw_phone = message.text.strip()
    digits = re.sub(r"\D", "", raw_phone)
    if len(digits) < 7 or len(digits) > 15:
        await message.answer(
            f"{tg_emoji('warning')} Некорректный номер! Введите полный номер с кодом страны (например, <code>+79991234567</code>):",
            parse_mode=ParseMode.HTML
        )
        return

    phone_formatted = f"+{digits}"
    session_file = SESSIONS_DIR / f"{digits}.session"

    api_id, api_hash = get_random_api_credentials()
    if not api_id or not api_hash:
        await message.answer(
            f"{tg_emoji('cross')} В пуле нет валидных API_ID/API_HASH. Проверьте конфигурацию .env.",
            parse_mode=ParseMode.HTML
        )
        await state.clear()
        return

    client = TelegramClient(str(session_file), api_id, api_hash)
    try:
        await client.connect()
        sent_code = await client.send_code_request(phone_formatted)
    except FloodWaitError as e:
        await client.disconnect()
        await state.clear()
        await message.answer(
            f"{tg_emoji('warning')} Telegram временно заблокировал отправку кода (FloodWait: {e.seconds} сек). Попробуйте позже.",
            parse_mode=ParseMode.HTML
        )
        return
    except Exception as e:
        await client.disconnect()
        await state.clear()
        await message.answer(
            f"{tg_emoji('cross')} Ошибка отправки кода на {phone_formatted}: <code>{e}</code>",
            parse_mode=ParseMode.HTML
        )
        return

    # Сохраняем клиента и данные в памяти
    active_auth_clients[message.from_user.id] = client
    await state.update_data(
        phone_digits=digits,
        phone_formatted=phone_formatted,
        phone_code_hash=sent_code.phone_code_hash
    )
    await state.set_state(AddSessionStates.waiting_for_code)

    text = (
        f"{tg_emoji('speaker')} <b>Код подтверждения отправлен на {phone_formatted}!</b>\n\n"
        "Отправьте полученный код из Telegram или SMS.\n"
        "<i>💡 Если код пришел с пробелами (например, 123 45), можете отправить как есть.</i>"
    )
    await message.answer(
        text,
        reply_markup=get_back_cancel_keyboard(to_where="menu_main"),
        parse_mode=ParseMode.HTML
    )

@router.message(AddSessionStates.waiting_for_code)
async def process_auth_code(message: types.Message, state: FSMContext, worker_mgr: WorkerManager):
    """Обработка ввода кода подтверждения."""
    if not await check_admin(message.from_user):
        return

    client = active_auth_clients.get(message.from_user.id)
    if not client:
        await state.clear()
        await message.answer(
            f"{tg_emoji('warning')} Сессия авторизации истекла или была прервана. Начните заново.",
            parse_mode=ParseMode.HTML
        )
        return

    data = await state.get_data()
    phone_formatted = data.get("phone_formatted")
    phone_code_hash = data.get("phone_code_hash")
    phone_digits = data.get("phone_digits")

    code = re.sub(r"\D", "", message.text.strip())
    if not code:
        await message.answer(
            f"{tg_emoji('warning')} Введите код подтверждения цифрами:",
            parse_mode=ParseMode.HTML
        )
        return

    try:
        await client.sign_in(phone=phone_formatted, code=code, phone_code_hash=phone_code_hash)
    except SessionPasswordNeededError:
        # На аккаунте включен 2FA пароль
        await state.set_state(AddSessionStates.waiting_for_password)
        text = (
            f"{tg_emoji('lock')} <b>На аккаунте включен двухфакторный пароль (2FA)!</b>\n\n"
            "Пожалуйста, отправьте ваш облачный пароль:"
        )
        await message.answer(
            text,
            reply_markup=get_back_cancel_keyboard(to_where="menu_main"),
            parse_mode=ParseMode.HTML
        )
        return
    except (PhoneCodeInvalidError, PhoneCodeExpiredError) as e:
        await message.answer(
            f"{tg_emoji('cross')} Неверный код или срок его действия истек ({e}). Попробуйте ввести еще раз:",
            parse_mode=ParseMode.HTML
        )
        return
    except Exception as e:
        await client.disconnect()
        active_auth_clients.pop(message.from_user.id, None)
        await state.clear()
        await message.answer(
            f"{tg_emoji('cross')} Ошибка входа: <code>{e}</code>",
            parse_mode=ParseMode.HTML
        )
        return

    # Успешный вход без 2FA
    me = await client.get_me()
    name = f"{me.first_name or ''} {me.last_name or ''}".strip()
    await client.disconnect()
    active_auth_clients.pop(message.from_user.id, None)
    await state.clear()

    await db.add_session(phone_digits)
    text = (
        f"{tg_emoji('check')} <b>Аккаунт успешно авторизован!</b>\n\n"
        f"{tg_emoji('user')} <b>Имя:</b> {name}\n"
        f"{tg_emoji('phone')} <b>Номер:</b> {phone_formatted}\n"
        f"📁 Сессия <code>{phone_digits}.session</code> сохранена и поставлена в очередь (READY)!"
    )
    is_running = worker_mgr.is_running()
    await message.answer(text, reply_markup=get_main_keyboard(is_running), parse_mode=ParseMode.HTML)

@router.message(AddSessionStates.waiting_for_password)
async def process_auth_password(message: types.Message, state: FSMContext, worker_mgr: WorkerManager):
    """Обработка ввода пароля двухфакторной аутентификации (2FA)."""
    if not await check_admin(message.from_user):
        return

    client = active_auth_clients.get(message.from_user.id)
    if not client:
        await state.clear()
        await message.answer(
            f"{tg_emoji('warning')} Сессия авторизации истекла. Начните заново.",
            parse_mode=ParseMode.HTML
        )
        return

    data = await state.get_data()
    phone_formatted = data.get("phone_formatted")
    phone_digits = data.get("phone_digits")
    password = message.text.strip()

    try:
        await client.sign_in(password=password)
    except PasswordHashInvalidError:
        await message.answer(
            f"{tg_emoji('cross')} Неверный облачный пароль! Попробуйте ввести еще раз:",
            parse_mode=ParseMode.HTML
        )
        return
    except Exception as e:
        await client.disconnect()
        active_auth_clients.pop(message.from_user.id, None)
        await state.clear()
        await message.answer(
            f"{tg_emoji('cross')} Ошибка авторизации: <code>{e}</code>",
            parse_mode=ParseMode.HTML
        )
        return

    # Успешный вход с 2FA
    me = await client.get_me()
    name = f"{me.first_name or ''} {me.last_name or ''}".strip()
    await client.disconnect()
    active_auth_clients.pop(message.from_user.id, None)
    await state.clear()

    await db.add_session(phone_digits)
    text = (
        f"{tg_emoji('check')} <b>Аккаунт успешно авторизован с 2FA!</b>\n\n"
        f"{tg_emoji('user')} <b>Имя:</b> {name}\n"
        f"{tg_emoji('phone')} <b>Номер:</b> {phone_formatted}\n"
        f"📁 Сессия <code>{phone_digits}.session</code> сохранена и поставлена в очередь (READY)!"
    )
    is_running = worker_mgr.is_running()
    await message.answer(text, reply_markup=get_main_keyboard(is_running), parse_mode=ParseMode.HTML)
