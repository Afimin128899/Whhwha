from __future__ import annotations
import os
import io
import re
import asyncio
import tempfile
import logging
from contextlib import suppress
from html import escape
from pathlib import Path
from aiogram import Router, F, types, Bot
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import SimpleEventIsolation
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
from bot.states import SettingsStates, AddSessionStates, ReceiverStates
from bot.emojis import tg_emoji, emoji_id
from bot.session_uploads import MAX_UPLOAD_BYTES, UploadError, stage_sessions
from worker.check_receiver import (
    ReceiverPool, RECEIVER_DIR, RECEIVER_SESSION, RECEIVER_BOT, receiver_setup_lock,
    MAX_RECEIVERS, receiver_session_path, receiver_account_key,
)

router = Router()
auth_isolation = SimpleEventIsolation()
AUTH_TIMEOUT_SECONDS = 300

# Временное хранилище Telethon клиентов в процессе авторизации (user_id -> TelegramClient)
active_auth_clients: dict[int, TelegramClient] = {}
receiver_auth_users: set[int] = set()
auth_timeouts: dict[int, asyncio.Task] = {}
upload_tasks: set[asyncio.Task] = set()
upload_lock = asyncio.Lock()
MAX_PENDING_UPLOADS = 5
UPLOAD_TIMEOUT_SECONDS = 120
upload_logger = logging.getLogger('session_uploads')


async def close_upload_tasks():
    tasks = list(upload_tasks)
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    upload_tasks.difference_update(tasks)


async def _upload_io(function, *args):
    # Cancellation must wait for the thread before deleting its files.
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        with suppress(Exception):
            await task
        raise


def _stage_download(source, filename, directory):
    with open(source, 'rb') as stream:
        return stage_sessions(stream, filename, directory)


def _publish_sessions(files, directory):
    names = []
    skipped = 0
    for staged in files:
        destination = directory / staged.name
        try:
            os.link(staged, destination)
        except FileExistsError:
            skipped += 1
        else:
            names.append(destination.stem)
    return names, skipped



async def cancel_auth(user_id: int):
    timer = auth_timeouts.pop(user_id, None)
    if timer and timer is not asyncio.current_task():
        timer.cancel()
    client = active_auth_clients.pop(user_id, None)
    if client:
        with suppress(Exception):
            await client.disconnect()
    if user_id in receiver_auth_users:
        receiver_auth_users.discard(user_id)
        try:
            for suffix in ('', '-journal', '-wal', '-shm'):
                (RECEIVER_DIR / f'pending_{user_id}.session{suffix}').unlink(missing_ok=True)
        finally:
            if receiver_setup_lock.locked():
                receiver_setup_lock.release()


async def close_auth_clients():
    for user_id in set(active_auth_clients) | receiver_auth_users | set(auth_timeouts):
        await cancel_auth(user_id)


def schedule_auth_timeout(user_id: int, state: FSMContext):
    previous = auth_timeouts.pop(user_id, None)
    if previous:
        previous.cancel()

    async def expire():
        await asyncio.sleep(AUTH_TIMEOUT_SECONDS)
        async with auth_isolation.lock(key=state.key):
            await cancel_auth(user_id)
            await state.clear()

    auth_timeouts[user_id] = asyncio.create_task(expire())


async def delete_auth_input(message: types.Message):
    with suppress(Exception):
        await message.delete()


def receiver_admin(user, chat) -> bool:
    return bool(user and chat and chat.type == 'private'
                and config.ADMIN_ID and user.id == config.ADMIN_ID)


def receiver_keyboard():
    return types.InlineKeyboardMarkup(inline_keyboard=[
        *[[types.InlineKeyboardButton(text=f'Получатель №{slot}: войти / заменить',
                                      callback_data=f'receiver_login:{slot}')]
          for slot in range(1, MAX_RECEIVERS + 1)],
        [types.InlineKeyboardButton(text='Включить автооткрытие', callback_data='receiver_enable')],
        [types.InlineKeyboardButton(text='Проверить подключение', callback_data='receiver_test')],
        [types.InlineKeyboardButton(text='Бот из ссылки чека', callback_data='receiver_set_bot')],
        [types.InlineKeyboardButton(text='Отключить автооткрытие', callback_data='receiver_disable')],
        [types.InlineKeyboardButton(text='Назад', callback_data='menu_main')],
    ])


@router.callback_query(F.data == 'receiver_menu')
async def cb_receiver_menu(callback: types.CallbackQuery, state: FSMContext):
    if not receiver_admin(callback.from_user, getattr(callback.message, 'chat', None)):
        return await callback.answer('Доступ только администратору в личном чате.', show_alert=True)
    await cancel_auth(callback.from_user.id)
    await state.clear()
    enabled = await db.get_setting('check_receiver_enabled', '')
    if enabled == '':
        enabled = os.getenv('CHECK_RECEIVER_ENABLED', '0')
    check_bot = await db.get_setting('check_receiver_bot', RECEIVER_BOT)
    status = 'включено' if enabled == '1' else 'выключено'
    saved = []
    for slot in range(1, MAX_RECEIVERS + 1):
        configured = receiver_session_path(slot, RECEIVER_DIR).is_file()
        saved_id = await db.get_setting(receiver_account_key(slot), '')
        saved.append(f'№{slot}: ' + (f'подключён, ID {escape(saved_id) or "неизвестен"}' if configured else 'не настроен'))
    await callback.message.answer(
        f'Аккаунты-получатели (до 5)\nАвтооткрытие: {status}.\n'
        + '\n'.join(saved) + '\n\n'
        f'Бот из ссылки чека: @{escape(check_bot)}.\n'
        'Вход и проверка остановят воркер. После настройки запусти его снова.\n'
        'Каждый чек отправляется одному получателю по очереди: 1 → 2 → … → 1.\n'
        'Получатели не добавляются в рабочую очередь.', reply_markup=receiver_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data == 'receiver_set_bot')
async def cb_receiver_set_bot(callback: types.CallbackQuery, state: FSMContext):
    if not receiver_admin(callback.from_user, getattr(callback.message, 'chat', None)):
        return await callback.answer('Нет доступа.', show_alert=True)
    await cancel_auth(callback.from_user.id)
    await state.clear()
    await state.set_state(ReceiverStates.waiting_for_bot)
    await callback.message.answer(
        'Пришли только @username бота из самой ссылки чека — часть после t.me/ до ?start=.\n'
        'Полную ссылку чека присылать не нужно. Например: @OnoGo_bot или @send.',
        reply_markup=get_back_cancel_keyboard(to_where='receiver_menu'),
    )
    await callback.answer()


@router.message(ReceiverStates.waiting_for_bot, F.text)
async def process_receiver_bot(message: types.Message, state: FSMContext, worker_mgr: WorkerManager):
    if not receiver_admin(message.from_user, message.chat):
        return
    username = message.text.strip().lstrip('@')
    if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{3,31}', username):
        return await message.answer('Пришли только @username бота, без ссылки и start-параметра.')
    async with receiver_setup_lock:
        await worker_mgr.stop()
        await db.set_setting('check_receiver_bot', username)
    await state.clear()
    await message.answer(
        f'Бот из ссылки: @{escape(username)}. Нажми «Проверить подключение», затем запусти воркер.',
        reply_markup=receiver_keyboard(),
    )


@router.callback_query((F.data == 'receiver_login') | F.data.startswith('receiver_login:'))
async def cb_receiver_login(callback: types.CallbackQuery, state: FSMContext, worker_mgr: WorkerManager):
    if not receiver_admin(callback.from_user, getattr(callback.message, 'chat', None)):
        return await callback.answer('Нет доступа.', show_alert=True)
    await cancel_auth(callback.from_user.id)
    if receiver_setup_lock.locked():
        return await callback.answer('Вход уже выполняется.', show_alert=True)
    data = getattr(callback, 'data', 'receiver_login')
    try:
        slot = int(data.split(':', 1)[1]) if ':' in data else 1
        receiver_session_path(slot, RECEIVER_DIR)
    except (ValueError, TypeError):
        return await callback.answer('Номер получателя должен быть от 1 до 5.', show_alert=True)
    await receiver_setup_lock.acquire()
    receiver_auth_users.add(callback.from_user.id)
    try:
        await worker_mgr.stop()
        RECEIVER_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        RECEIVER_DIR.chmod(0o700)
        for suffix in ('', '-journal', '-wal', '-shm'):
            (RECEIVER_DIR / f'pending_{callback.from_user.id}.session{suffix}').unlink(missing_ok=True)
        await state.clear()
        await state.update_data(auth_target='receiver', receiver_slot=slot)
        await state.set_state(AddSessionStates.waiting_for_phone)
        schedule_auth_timeout(callback.from_user.id, state)
        await callback.message.answer(
            f'Вход в аккаунт-получатель №{slot}. Отправь номер с кодом страны.\n'
            'На вход есть 5 минут. Предыдущий получатель сохранится до успешного входа.\n'
            'Сообщения с номером, кодом и паролем бот попробует удалить после обработки.',
            reply_markup=get_back_cancel_keyboard(to_where='receiver_menu'),
        )
        await callback.answer()
    except BaseException:
        await cancel_auth(callback.from_user.id)
        await state.clear()
        raise


@router.callback_query(F.data == 'receiver_enable')
async def cb_receiver_enable(callback: types.CallbackQuery, state: FSMContext, worker_mgr: WorkerManager):
    if not receiver_admin(callback.from_user, getattr(callback.message, 'chat', None)):
        return await callback.answer('Нет доступа.', show_alert=True)
    await cancel_auth(callback.from_user.id)
    await worker_mgr.stop()
    if not any(receiver_session_path(slot, RECEIVER_DIR).is_file()
               for slot in range(1, MAX_RECEIVERS + 1)):
        return await callback.answer('Сначала добавь хотя бы одного получателя.', show_alert=True)
    await db.set_setting('check_receiver_enabled', '1')
    await cb_receiver_menu(callback, state)


@router.callback_query(F.data == 'receiver_disable')
async def cb_receiver_disable(callback: types.CallbackQuery, state: FSMContext, worker_mgr: WorkerManager):
    if not receiver_admin(callback.from_user, getattr(callback.message, 'chat', None)):
        return await callback.answer('Нет доступа.', show_alert=True)
    await cancel_auth(callback.from_user.id)
    await worker_mgr.stop()
    await db.set_setting('check_receiver_enabled', '0')
    await cb_receiver_menu(callback, state)


@router.callback_query(F.data == 'receiver_test')
async def cb_receiver_test(callback: types.CallbackQuery, state: FSMContext, worker_mgr: WorkerManager):
    if not receiver_admin(callback.from_user, getattr(callback.message, 'chat', None)):
        return await callback.answer('Нет доступа.', show_alert=True)
    await cancel_auth(callback.from_user.id)
    await state.clear()
    await callback.answer('Проверяю подключение…')
    receiver = ReceiverPool()
    await receiver_setup_lock.acquire()
    try:
        await worker_mgr.stop()
        await receiver.connect()
        accounts = ', '.join(f'№{slot}: ID {account_id}' for slot, account_id in receiver.account_ids.items())
        await callback.message.answer(
            f'Получатели подключены: {accounts}. Бот чеков доступен.\n'
            'Проверка не открывала чеки. Теперь можно запустить воркер.',
            reply_markup=receiver_keyboard(),
        )
    except Exception as exc:
        await callback.message.answer(
            f'Подключение не удалось: {escape(type(exc).__name__)}.\n'
            'Проверь авторизацию через «Войти по номеру».', reply_markup=receiver_keyboard(),
        )
    finally:
        try:
            await receiver.close()
        finally:
            receiver_setup_lock.release()

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

    await cancel_auth(message.from_user.id)
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
        f"{tg_emoji('idea')} <i>Отправляй .session или .zip с сессиями прямо в этот чат — они добавятся в очередь.</i>"
    )
    await message.answer(text, reply_markup=get_main_keyboard(is_running), parse_mode=ParseMode.HTML)

@router.callback_query(F.data == "menu_main")
async def cb_menu_main(callback: types.CallbackQuery, state: FSMContext | None = None, worker_mgr: WorkerManager | None = None):
    """Возврат в главное меню."""
    if not await check_admin(callback.from_user):
        return await callback.answer("Нет доступа", show_alert=True)

    if state is not None:
        await cancel_auth(callback.from_user.id)
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
        f"{tg_emoji('idea')} <i>Отправляй .session или .zip с сессиями прямо в этот чат — они добавятся в очередь.</i>"
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

    if receiver_setup_lock.locked():
        return await callback.answer('Сначала заверши или отмени вход получателя.', show_alert=True)

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
        lines.append(f"{i}. <code>{c['session_name']}</code> | {c['amount']}$ -> {c['check_url']}\n{c.get('activation_note', 'Активация не проверялась.').splitlines()[0]}")

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

    await cancel_auth(callback.from_user.id)
    await state.clear()
    settings = await db.get_all_settings()
    text = (
        f"{tg_emoji('settings')} <b>Текущие настройки системы:</b>\n\n"
        f"{tg_emoji('link')} <b>Реферальная ссылка:</b>\n<code>{settings.get('ref_link', '')}</code>\n\n"
        f"{tg_emoji('speaker')} <b>Каналы для подписки:</b> <code>{settings.get('channels', '')}</code>\n"
        f"{tg_emoji('dollar')} <b>Сумма чека:</b> <code>{settings.get('check_amount', '0.1')}$</code>\n"
        f"{tg_emoji('stopwatch')} <b>Интервал пауз:</b> <code>{settings.get('delay_min', '2')} - {settings.get('delay_max', '5')} сек</code>\n\n"
        "Рабочих аккаунтов одновременно: "
        f"<b>{'по очереди' if settings.get('sequential_accounts', '0') == '1' else settings.get('worker_count', '5')}</b> (со следующего запуска).\n"
        "Активация чеков: не чаще одного раза в 10 секунд.\n\n"
        "<i>Нажмите на кнопку ниже, чтобы изменить соответствующий параметр.</i>"
    )
    await callback.message.edit_text(text, reply_markup=get_settings_keyboard(settings), parse_mode=ParseMode.HTML)
    await callback.answer()

@router.callback_query(F.data == "toggle_worker_count")
async def cb_toggle_worker_count(callback: types.CallbackQuery, state: FSMContext):
    if not await check_admin(callback.from_user):
        return await callback.answer("Нет доступа", show_alert=True)
    current = await db.get_setting("worker_count", "5")
    sequential = await db.get_setting("sequential_accounts", "0")
    if sequential == "1":
        await db.set_setting("worker_count", "5")
        await db.set_setting("sequential_accounts", "0")
    elif current == "5":
        await db.set_setting("worker_count", "10")
    else:
        await db.set_setting("sequential_accounts", "1")
    await cb_menu_settings(callback, state)


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

# --- ПРИЕМ ФАЙЛОВ СЕССИЙ (.session / .zip) ---

@router.message(F.document)
async def handle_session_file(message: types.Message, bot: Bot):
    """Import individual sessions or an archive, preserving existing files."""
    if not await check_admin(message.from_user):
        return
    doc = message.document
    filename = doc.file_name or ''
    if Path(filename).suffix.lower() not in ('.session', '.zip'):
        return await message.answer('Принимаются .session и .zip с файлами сессий.')
    if doc.file_size and doc.file_size > MAX_UPLOAD_BYTES:
        return await message.answer('Максимальный размер загрузки — 20 МБ.')
    if len(upload_tasks) >= MAX_PENDING_UPLOADS:
        return await message.answer('Очередь загрузок заполнена. Дождись результата предыдущих файлов.')
    await message.answer('Файл принят в очередь загрузки. Меню доступно; результат пришлю отдельно.')
    task = asyncio.create_task(_run_session_upload(message, bot, doc, filename))
    upload_tasks.add(task)
    task.add_done_callback(upload_tasks.discard)


async def _run_session_upload(message, bot, doc, filename):
    try:
        async with upload_lock:
            SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix='.upload-', dir=SESSIONS_DIR) as staging:
                staging = Path(staging)
                source = staging / 'download'
                # A path destination makes aiogram write through aiofiles.
                async with asyncio.timeout(UPLOAD_TIMEOUT_SECONDS):
                    await bot.download(doc, destination=source)
                files_dir = staging / 'files'
                files_dir.mkdir()
                files = await _upload_io(_stage_download, source, filename, files_dir)
                names, skipped = await _upload_io(_publish_sessions, files, SESSIONS_DIR)
                added = await db.add_sessions(names)
                skipped += len(names) - added
        await message.answer(
            f'Файл <code>{escape(filename)}</code>\nДобавлено в очередь: {added}. '
            f'Уже существуют / пропущено: {skipped}.\nСуществующие сессии не заменяются.',
            parse_mode=ParseMode.HTML,
        )
    except UploadError as exc:
        await _report_upload_error(message, f'Загрузка отклонена: {escape(str(exc))}')
    except TimeoutError:
        await _report_upload_error(message, 'Скачивание заняло больше 120 секунд. Отправь файл ещё раз.')
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        upload_logger.error('Ошибка импорта сессий: %s', type(exc).__name__)
        await _report_upload_error(message,
            'Не удалось загрузить файл. Проверь соединение и свободное место на сервере, затем повтори.')


async def _report_upload_error(message, text):
    try:
        await message.answer(text)
    except Exception as exc:
        upload_logger.error('Не удалось сообщить об ошибке загрузки: %s', type(exc).__name__)

# --- АВТОРИЗАЦИЯ ПО НОМЕРУ И КОДУ ---

@router.callback_query(F.data == "add_session_phone")
async def cb_add_session_phone(callback: types.CallbackQuery, state: FSMContext):
    """Начало процесса входа по номеру телефона."""
    if not await check_admin(callback.from_user):
        return await callback.answer("Нет доступа", show_alert=True)

    # Закрываем предыдущую незавершенную попытку, если была
    await cancel_auth(callback.from_user.id)
    await state.clear()
    schedule_auth_timeout(callback.from_user.id, state)
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

    if not message.text:
        return
    data = await state.get_data()
    is_receiver = data.get('auth_target') == 'receiver'
    if is_receiver and not receiver_admin(message.from_user, message.chat):
        return
    raw_phone = message.text.strip()
    if is_receiver:
        await delete_auth_input(message)
    digits = re.sub(r"\D", "", raw_phone)
    if len(digits) < 7 or len(digits) > 15:
        await message.answer(
            f"{tg_emoji('warning')} Некорректный номер! Введите полный номер с кодом страны (например, <code>+79991234567</code>):",
            parse_mode=ParseMode.HTML
        )
        return

    phone_formatted = f"+{digits}"
    session_file = SESSIONS_DIR / f"{digits}.session"
    if is_receiver:
        if message.from_user.id not in receiver_auth_users:
            await state.clear()
            return await message.answer('Время входа истекло. Начни заново в меню получателя.')
        session_file = RECEIVER_DIR / f'pending_{message.from_user.id}.session'

    api_id, api_hash = get_random_api_credentials()
    if not api_id or not api_hash:
        await message.answer(
            f"{tg_emoji('cross')} В пуле нет валидных API_ID/API_HASH. Проверьте конфигурацию .env.",
            parse_mode=ParseMode.HTML
        )
        await cancel_auth(message.from_user.id)
        await state.clear()
        return

    client = TelegramClient(
        str(session_file), api_id, api_hash,
        lang_code="en", system_lang_code="en-US",
    )
    active_auth_clients[message.from_user.id] = client
    try:
        await client.connect()
        if is_receiver and session_file.exists():
            session_file.chmod(0o600)
        sent_code = await client.send_code_request(phone_formatted)
    except FloodWaitError as e:
        await cancel_auth(message.from_user.id)
        await state.clear()
        await message.answer(
            f"{tg_emoji('warning')} Telegram временно заблокировал отправку кода (FloodWait: {e.seconds} сек). Попробуйте позже.",
            parse_mode=ParseMode.HTML
        )
        return
    except Exception as e:
        await cancel_auth(message.from_user.id)
        await state.clear()
        await message.answer(
            f"{tg_emoji('cross')} Ошибка отправки кода: <code>{type(e).__name__}</code>",
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
        f"{tg_emoji('speaker')} <b>Код подтверждения запрошен!</b>\n\n"
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
    if not message.text:
        return
    if data.get('auth_target') == 'receiver':
        if not receiver_admin(message.from_user, message.chat):
            return
        await delete_auth_input(message)
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
    except PhoneCodeExpiredError:
        await cancel_auth(message.from_user.id)
        await state.clear()
        keyboard = receiver_keyboard() if data.get('auth_target') == 'receiver' else get_main_keyboard(False)
        await message.answer('Код истёк. Запроси новый через кнопку входа.', reply_markup=keyboard)
        return
    except PhoneCodeInvalidError:
        await message.answer(
            f"{tg_emoji('cross')} Неверный код. Попробуйте ввести ещё раз:",
            parse_mode=ParseMode.HTML
        )
        return
    except Exception as e:
        await cancel_auth(message.from_user.id)
        await state.clear()
        await message.answer(
            f"{tg_emoji('cross')} Ошибка входа: <code>{type(e).__name__}</code>",
            parse_mode=ParseMode.HTML
        )
        return

    if data.get('auth_target') == 'receiver':
        await finish_receiver_auth(message, state)
        return

    # Успешный вход без 2FA
    me = await client.get_me()
    name = f"{me.first_name or ''} {me.last_name or ''}".strip()
    await cancel_auth(message.from_user.id)
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
    if not message.text:
        return
    if data.get('auth_target') == 'receiver':
        if not receiver_admin(message.from_user, message.chat):
            return
        await delete_auth_input(message)
    phone_formatted = data.get("phone_formatted")
    phone_digits = data.get("phone_digits")
    password = message.text

    try:
        await client.sign_in(password=password)
    except PasswordHashInvalidError:
        await message.answer(
            f"{tg_emoji('cross')} Неверный облачный пароль! Попробуйте ввести еще раз:",
            parse_mode=ParseMode.HTML
        )
        return
    except Exception as e:
        await cancel_auth(message.from_user.id)
        await state.clear()
        await message.answer(
            f"{tg_emoji('cross')} Ошибка авторизации: <code>{type(e).__name__}</code>",
            parse_mode=ParseMode.HTML
        )
        return

    if data.get('auth_target') == 'receiver':
        await finish_receiver_auth(message, state)
        return

    # Успешный вход с 2FA
    me = await client.get_me()
    name = f"{me.first_name or ''} {me.last_name or ''}".strip()
    await cancel_auth(message.from_user.id)
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


async def finish_receiver_auth(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    client = active_auth_clients[user_id]
    try:
        me = await client.get_me()
        if not me or me.bot:
            raise ValueError('Expected a user account')
        await client.disconnect()
        active_auth_clients.pop(user_id, None)
        source = RECEIVER_DIR / f'pending_{user_id}.session'
        data = await state.get_data()
        slot = data.get('receiver_slot', 1)
        for other in range(1, MAX_RECEIVERS + 1):
            if other != slot and await db.get_setting(receiver_account_key(other), '') == str(me.id):
                raise ValueError('Этот аккаунт уже добавлен в другой слот')
        destination = receiver_session_path(slot, RECEIVER_DIR)
        os.replace(source, destination)
        destination.chmod(0o600)
        await db.set_setting(receiver_account_key(slot), str(me.id))
        await db.set_setting('check_receiver_enabled', '1')
    except ValueError as exc:
        await message.answer(f'Получатель не сохранён: {escape(str(exc))}.')
        return
    except Exception as exc:
        await message.answer(f'Не удалось сохранить получателя: {type(exc).__name__}. Повтори вход.')
        return
    finally:
        await cancel_auth(user_id)
        await state.clear()
    await message.answer(
        f'Получатель №{slot} подключён: Telegram ID {me.id}. Автооткрытие включено.\n'
        'Аккаунт сохранён отдельно от рабочей очереди. Запусти воркер в главном меню.',
        reply_markup=receiver_keyboard(),
    )
