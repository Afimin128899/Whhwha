from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from typing import Dict
from bot.emojis import emoji_id

def get_main_keyboard(is_running: bool) -> InlineKeyboardMarkup:
    """Главная клавиатура админ-панели с Premium Custom Emoji."""
    if is_running:
        run_btn = InlineKeyboardButton(
            text="Остановить воркер",
            callback_data="worker_stop",
            icon_custom_emoji_id=emoji_id("stop")
        )
    else:
        run_btn = InlineKeyboardButton(
            text="Запустить воркер",
            callback_data="worker_start",
            icon_custom_emoji_id=emoji_id("play")
        )

    keyboard = [
        [run_btn],
        [
            InlineKeyboardButton(
                text="Статистика",
                callback_data="view_stats",
                icon_custom_emoji_id=emoji_id("stats")
            ),
            InlineKeyboardButton(
                text="Чеки",
                callback_data="view_checks",
                icon_custom_emoji_id=emoji_id("dollar")
            )
        ],
        [
            InlineKeyboardButton(
                text="Добавить по номеру",
                callback_data="add_session_phone",
                icon_custom_emoji_id=emoji_id("phone")
            ),
            InlineKeyboardButton(
                text="Выгрузить чеки (.txt)",
                callback_data="export_checks_file",
                icon_custom_emoji_id=emoji_id("download")
            )
        ],
        [
            InlineKeyboardButton(
                text="Сканировать папку",
                callback_data="scan_sessions",
                icon_custom_emoji_id=emoji_id("folder")
            ),
            InlineKeyboardButton(
                text="Сброс в READY",
                callback_data="reset_sessions_confirm",
                icon_custom_emoji_id=emoji_id("repeat")
            )
        ],
        [
            InlineKeyboardButton(
                text="Настройки",
                callback_data="menu_settings",
                icon_custom_emoji_id=emoji_id("settings")
            )
        ]
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

def get_settings_keyboard(settings: Dict[str, str]) -> InlineKeyboardMarkup:
    """Клавиатура меню настроек с Premium Custom Emoji."""
    ref_link_short = settings.get("ref_link", "")
    if len(ref_link_short) > 25:
        ref_link_short = ref_link_short[:22] + "..."

    keyboard = [
        [
            InlineKeyboardButton(
                text=f"Рефка: {ref_link_short}",
                callback_data="set_ref_link",
                icon_custom_emoji_id=emoji_id("link")
            )
        ],
        [
            InlineKeyboardButton(
                text=f"Каналы: {settings.get('channels', '')}",
                callback_data="set_channels",
                icon_custom_emoji_id=emoji_id("speaker")
            )
        ],
        [
            InlineKeyboardButton(
                text=f"Сумма: {settings.get('check_amount', '0.1')}$",
                callback_data="set_check_amount",
                icon_custom_emoji_id=emoji_id("dollar")
            ),
            InlineKeyboardButton(
                text=f"Паузы: {settings.get('delay_min', '2')}-{settings.get('delay_max', '5')}с",
                callback_data="set_delays",
                icon_custom_emoji_id=emoji_id("stopwatch")
            )
        ],
        [
            InlineKeyboardButton(
                text="Назад в меню",
                callback_data="menu_main",
                icon_custom_emoji_id=emoji_id("back")
            )
        ]
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

def get_back_cancel_keyboard(to_where: str = "menu_settings") -> InlineKeyboardMarkup:
    """Кнопка отмены ввода с Premium Custom Emoji."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="Отмена",
                callback_data=to_where,
                icon_custom_emoji_id=emoji_id("cross")
            )
        ]
    ])

def get_reset_confirm_keyboard() -> InlineKeyboardMarkup:
    """Подтверждение сброса сессий с Premium Custom Emoji."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="Да, сбросить все",
                callback_data="reset_sessions_do",
                icon_custom_emoji_id=emoji_id("check")
            ),
            InlineKeyboardButton(
                text="Отмена",
                callback_data="menu_main",
                icon_custom_emoji_id=emoji_id("cross")
            )
        ]
    ])
