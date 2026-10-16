import asyncio
import logging
import random
import re
from pathlib import Path
from typing import Tuple, Optional, List
from urllib.parse import urlparse, parse_qs

from telethon import TelegramClient
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.errors import (
    FloodWaitError,
    SessionPasswordNeededError,
    UserDeactivatedError,
    AuthKeyUnregisteredError,
    UserBannedInChannelError
)

from config import get_random_api_credentials, SESSIONS_DIR

logger = logging.getLogger("worker")

_BALANCE_RE = re.compile(r"Баланс:\s*([0-9]+(?:[.,][0-9]+)?)", re.IGNORECASE)
_DOLLAR_RE = re.compile(r"([0-9]+(?:[.,][0-9]+)?)\s*\$")


def _to_float(raw: str) -> Optional[float]:
    try:
        return float(raw.replace(",", "."))
    except ValueError:
        return None


def parse_balance(text: str) -> Optional[float]:
    """Извлекает число после 'Баланс:'. Возвращает None, если число не найдено."""
    match = _BALANCE_RE.search(text or "")
    return _to_float(match.group(1)) if match else None


def parse_dollar_amount(text: str) -> Optional[float]:
    """Извлекает число перед знаком доллара. Возвращает None, если число не найдено."""
    match = _DOLLAR_RE.search(text or "")
    return _to_float(match.group(1)) if match else None


async def random_delay(delay_min: float, delay_max: float):
    """Случайная задержка между действиями для защиты от спамблока."""
    await asyncio.sleep(random.uniform(delay_min, delay_max))

def parse_bot_and_payload(ref_link: str) -> Tuple[str, str]:
    """
    Извлекает юзернейм бота и start-параметр из реферальной ссылки.
    Пример: https://t.me/redcubebetbot?start=CRn6TH6FUBTy -> ('redcubebetbot', 'CRn6TH6FUBTy')
    """
    clean_url = ref_link.strip()
    if not clean_url.startswith("http"):
        clean_url = "https://" + clean_url

    parsed = urlparse(clean_url)
    bot_username = parsed.path.strip("/").split("/")[-1]
    query_params = parse_qs(parsed.query)
    start_payload = query_params.get("start", [""])[0]

    if not bot_username:
        bot_username = "redcubebetbot"

    return bot_username, start_payload

async def wait_for_bot_update(client: TelegramClient, bot_entity, prev_msg=None, timeout: int = 10):
    """
    Ожидание реакции бота:
    - Появление нового сообщения (msg.id > prev_msg.id)
    - Или изменение текста / кнопок текущего сообщения (inline edit)
    """
    start_time = asyncio.get_event_loop().time()
    prev_id = prev_msg.id if prev_msg else 0
    prev_text = prev_msg.text if prev_msg else ""
    prev_buttons_str = str(prev_msg.buttons) if prev_msg and prev_msg.buttons else ""

    while asyncio.get_event_loop().time() - start_time < timeout:
        messages = await client.get_messages(bot_entity, limit=4)
        for msg in messages:
            if not msg.out:
                # 1. Появилось новое сообщение от бота
                if msg.id > prev_id:
                    return msg
                # 2. Текущее сообщение было отредактировано ботом (текст или кнопки изменились)
                if msg.id == prev_id:
                    curr_buttons_str = str(msg.buttons) if msg.buttons else ""
                    if msg.text != prev_text or curr_buttons_str != prev_buttons_str:
                        return msg
        await asyncio.sleep(0.7)

    # Если таймаут истек, возвращаем самое свежее сообщение
    messages = await client.get_messages(bot_entity, limit=1)
    if messages and not messages[0].out:
        return messages[0]
    return prev_msg

async def click_button_with_alert(msg, patterns: List[str]) -> Tuple[bool, Optional[str]]:
    """
    Поиск и нажатие на inline или reply кнопку по шаблонам/тексту.
    Возвращает (clicked, alert_text)
    """
    if not msg:
        return False, None

    # 1. Попытка нажать на inline-кнопку
    if msg.buttons:
        for row in msg.buttons:
            for btn in row:
                btn_text = btn.text.strip()
                for pattern in patterns:
                    if pattern.lower() in btn_text.lower():
                        logger.info(f"Нажатие inline-кнопки: '{btn_text}'")
                        try:
                            res = await btn.click()
                            alert_msg = getattr(res, "message", None)
                            if alert_msg:
                                logger.info(f"Ответ бота на кнопку '{btn_text}': {alert_msg}")
                            return True, alert_msg
                        except Exception as e:
                            logger.warning(f"Ошибка при клике на inline кнопку '{btn_text}': {e}")
                            return False, str(e)

    # 2. Попытка клика через API Telethon по тексту
    for pattern in patterns:
        try:
            res = await msg.click(text=re.compile(pattern, re.IGNORECASE))
            logger.info(f"Успешный клик по шаблону: {pattern}")
            alert_msg = getattr(res, "message", None)
            return True, alert_msg
        except Exception:
            continue

    return False, None

def extract_check_url(msg) -> Optional[str]:
    """Извлечение ссылки на чек из сообщения."""
    if not msg:
        return None
    full_text = (msg.text or "") + "\n" + (msg.raw_text or "")
    match = re.search(r"https://t\.me/(?:[a-zA-Z0-9_]+)\?start=([a-zA-Z0-9_-]+)", full_text)
    if match:
        return match.group(0)
    if msg.entities:
        for entity, text in msg.get_entities_text():
            if "http" in text and "start=" in text:
                return text
    return None

async def process_account_telethon(
    session_name: str,
    ref_link: str,
    channels: List[str],
    check_amount: str = "0.1",
    check_count: str = "1",
    delay_min: float = 2.0,
    delay_max: float = 4.0
) -> Tuple[str, str, Optional[str]]:
    """
    Основной пайплайн обработки аккаунта:
    1. Авторизация в сессии
    2. Подписка на каналы
    3. Отправка /start <ref>
    4. Нажатие кнопки 'Чеки' -> 'Создать чек'
    5. Проверка баланса (если 0 - алерт)
    6. Отправка в чат суммы (0.1)
    7. Отправка в чат количества активаций (1)
    8. Парсинг чека и возврат результата
    """
    session_file = SESSIONS_DIR / f"{session_name}.session"
    if not session_file.exists():
        session_file = SESSIONS_DIR / session_name
        if not session_file.exists():
            return "ERROR", f"Файл сессии {session_name} не найден на диске", None

    api_id, api_hash = get_random_api_credentials()
    if not api_id or not api_hash:
        return "ERROR", "В пуле нет валидных API_ID и API_HASH", None

    client = TelegramClient(str(session_file), api_id, api_hash)

    try:
        await client.connect()
        if not await client.is_user_authorized():
            return "INVALID", "Сессия не авторизована или сброшена", None

        me = await client.get_me()
        user_info = f"{me.first_name or ''} (@{me.username or me.phone})"
        logger.info(f"[{session_name}] Авторизован как: {user_info}")

        # --- 1. Подписка на обязательные каналы и чаты ---
        for ch in channels:
            ch_clean = ch.strip().replace("https://t.me/", "").replace("@", "")
            if not ch_clean:
                continue
            try:
                ch_entity = await client.get_entity(ch_clean)
                await client(JoinChannelRequest(ch_entity))
                logger.info(f"[{session_name}] Подписан на {ch_clean}")
                await random_delay(delay_min, delay_max)
            except FloodWaitError as e:
                return "ERROR", f"FloodWait при подписке на {ch_clean}: {e.seconds} сек", None
            except Exception as e:
                logger.warning(f"[{session_name}] Ошибка подписки на {ch_clean}: {e}")

        # --- 2. Запуск бота по реферальной ссылке ---
        bot_username, start_payload = parse_bot_and_payload(ref_link)
        try:
            bot_entity = await client.get_entity(bot_username)
        except Exception as e:
            return "ERROR", f"Не удалось найти бота @{bot_username}: {e}", None

        start_command = f"/start {start_payload}".strip()
        last_sent = await client.send_message(bot_entity, start_command)
        logger.info(f"[{session_name}] Отправлено: {start_command} в @{bot_username}")
        await random_delay(delay_min, delay_max)

        # Ожидание ответа бота на /start
        resp_msg = await wait_for_bot_update(client, bot_entity, last_sent, timeout=10)
        if not resp_msg or resp_msg.out:
            return "ERROR", "Бот не ответил на команду /start", None

        # Проверка обязательной подписки бота ("Подпишись на каналы", кнопка "Проверить")
        full_raw = (resp_msg.text or "") + " " + (resp_msg.raw_text or "")
        if "подпиш" in full_raw.lower() or "проверить" in full_raw.lower():
            logger.info(f"[{session_name}] Обнаружено требование проверки подписки, нажимаем 'Проверить'...")
            clicked, _ = await click_button_with_alert(resp_msg, ["Проверить", "✅ Проверить", "Check"])
            if clicked:
                await random_delay(delay_min, delay_max)
                resp_msg = await wait_for_bot_update(client, bot_entity, resp_msg, timeout=10)

        # --- 3. Переход в меню 'Чеки' ---
        clicked_checks, _ = await click_button_with_alert(resp_msg, ["Чеки", "🧾 Чеки", "Чек"])

        if not clicked_checks:
            logger.info(f"[{session_name}] Кнопка Чеки не найдена, отправляем /start повторно")
            sent_start2 = await client.send_message(bot_entity, "/start")
            await random_delay(delay_min, delay_max)
            resp_msg = await wait_for_bot_update(client, bot_entity, sent_start2, timeout=10)
            if resp_msg:
                clicked_checks, _ = await click_button_with_alert(resp_msg, ["Чеки", "🧾 Чеки", "Чек"])

        if not clicked_checks:
            return "ERROR", "Не удалось найти/нажать кнопку 'Чеки'", None

        logger.info(f"[{session_name}] Нажата кнопка 'Чеки', ожидаем меню создания чека...")
        await random_delay(delay_min, delay_max)
        resp_msg = await wait_for_bot_update(client, bot_entity, resp_msg, timeout=10)

        # --- 4. Нажатие 'Создать чек' ---
        clicked_create, alert_create = await click_button_with_alert(resp_msg, ["Создать чек", "Создать"])
        if not clicked_create:
            # Проверим свежие сообщения
            messages = await client.get_messages(bot_entity, limit=2)
            for m in messages:
                if not m.out:
                    clicked_create, alert_create = await click_button_with_alert(m, ["Создать чек", "Создать"])
                    if clicked_create:
                        resp_msg = m
                        break

        # Проверка алерта от бота при нажатии на кнопку "Создать чек"
        if alert_create:
            logger.warning(f"[{session_name}] Алерт бота: {alert_create}")
            alert_lower = alert_create.lower()
            alert_balance = parse_balance(alert_create)
            if alert_balance is None:
                alert_balance = parse_dollar_amount(alert_create)
            logger.info(f"[{session_name}] Баланс из алерта: {alert_balance}")
            if "недостаточно" in alert_lower or (alert_balance is not None and alert_balance <= 0):
                return "ERROR", f"На аккаунте нет баланса для чека: {alert_create}", None

        if not clicked_create:
            return "ERROR", "Не удалось нажать кнопку 'Создать чек'", None

        logger.info(f"[{session_name}] Нажата кнопка 'Создать чек', ожидаем запрос суммы...")
        await random_delay(delay_min, delay_max)
        resp_msg = await wait_for_bot_update(client, bot_entity, resp_msg, timeout=10)

        # Проверка баланса в тексте сообщения
        msg_text = (resp_msg.text or "") if resp_msg else ""
        balance = parse_balance(msg_text)
        logger.info(f"[{session_name}] Распознанный баланс: {balance}")
        if (balance is not None and balance <= 0) or "Недостаточно" in msg_text:
            return "ERROR", f"На аккаунте нулевой баланс: {msg_text[:60]}", None

        # --- 5. Отправка суммы чека ТЕКСТОМ В ЧАТ (например, 0.1) ---
        logger.info(f"[{session_name}] Пишем в чат сумму чека: {check_amount}")
        sent_amount = await client.send_message(bot_entity, str(check_amount))
        await random_delay(delay_min, delay_max)
        resp_msg = await wait_for_bot_update(client, bot_entity, sent_amount, timeout=10)

        # --- 6. Отправка количества активаций ТЕКСТОМ В ЧАТ (например, 1) ---
        logger.info(f"[{session_name}] Пишем в чат количество активаций: {check_count}")
        sent_count = await client.send_message(bot_entity, str(check_count))
        await random_delay(delay_min, delay_max)
        resp_msg = await wait_for_bot_update(client, bot_entity, sent_count, timeout=12)

        # --- 7. Извлечение ссылки на чек ---
        check_url = extract_check_url(resp_msg)
        if not check_url:
            # Проверяем последние 3 сообщения в диалоге
            messages = await client.get_messages(bot_entity, limit=3)
            for m in messages:
                if not m.out:
                    check_url = extract_check_url(m)
                    if check_url:
                        break

        if not check_url:
            raw_preview = (resp_msg.text or "")[:80] if resp_msg else "нет текста"
            return "ERROR", f"Не удалось извлечь ссылку на чек: {raw_preview}...", None

        logger.info(f"[{session_name}] УСПЕХ! Создан чек: {check_url}")

        # Аккаунт НЕ очищаем и НЕ удаляем сессию: диалог и подписки сохраняются
        return "DONE", "Чек успешно создан", check_url

    except FloodWaitError as e:
        return "ERROR", f"FloodWait на аккаунте: {e.seconds} сек", None
    except (SessionPasswordNeededError, UserDeactivatedError, AuthKeyUnregisteredError) as e:
        return "INVALID", f"Сессия недействительна: {e}", None
    except Exception as e:
        logger.error(f"[{session_name}] Критическая ошибка: {e}", exc_info=True)
        return "ERROR", f"Исключение: {str(e)}", None
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass
