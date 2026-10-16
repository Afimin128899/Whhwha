import asyncio
import logging
from aiogram import Bot
from aiogram.enums import ParseMode
import config
from database.db import db
from worker.telethon_worker import process_account_telethon
from bot.emojis import tg_emoji

logger = logging.getLogger("worker_manager")

class WorkerManager:
    def __init__(self, bot: Bot):
        self.bot = bot
        self._task: asyncio.Task = None
        self._is_active = False

    async def start(self):
        """Запуск фонового воркера."""
        if self._is_active:
            return False
        self._is_active = True
        await db.set_setting("is_running", "1")
        self._task = asyncio.create_task(self._worker_loop())
        logger.info("Менеджер воркеров запущен")
        return True

    async def stop(self):
        """Остановка фонового воркера."""
        self._is_active = False
        await db.set_setting("is_running", "0")
        if self._task and not self._task.done():
            self._task.cancel()
        # Возвращаем сессии со статусом PROCESSING обратно в READY
        await db.reset_processing_sessions()
        logger.info("Менеджер воркеров остановлен")
        return True

    def is_running(self) -> bool:
        return self._is_active

    async def _worker_loop(self):
        """Бесконечный цикл обработки очереди сессий."""
        logger.info("Рабочий цикл воркера активирован")
        try:
            while self._is_active:
                # Получаем актуальные настройки из БД
                settings = await db.get_all_settings()
                if settings.get("is_running") != "1":
                    self._is_active = False
                    break

                # Ищем сессию в статусе READY
                session_name = await db.get_next_ready_session()
                if not session_name:
                    logger.info("Все готовые сессии обработаны. Воркер переходит в режим ожидания.")
                    if config.ADMIN_ID:
                        try:
                            await self.bot.send_message(
                                config.ADMIN_ID,
                                f"{tg_emoji('check')} <b>Все сессии обработаны!</b>\n"
                                "Очередь пуста. Загрузите новые сессии или проверьте статус в меню.",
                                parse_mode=ParseMode.HTML
                            )
                        except Exception as e:
                            logger.error(f"Не удалось отправить уведомление админу: {e}")
                    self._is_active = False
                    await db.set_setting("is_running", "0")
                    break

                # Переводим в статус PROCESSING
                await db.update_session_status(session_name, "PROCESSING", "В процессе обработки...")
                logger.info(f"Начата обработка сессии: {session_name}")

                # Параметры выполнения
                ref_link = settings.get("ref_link", "")
                channels_str = settings.get("channels", "")
                channels = [c.strip() for c in channels_str.split(",") if c.strip()]
                check_amount = settings.get("check_amount", "0.1")
                check_count = settings.get("check_count", "1")
                try:
                    delay_min = float(settings.get("delay_min", "2"))
                    delay_max = float(settings.get("delay_max", "5"))
                except ValueError:
                    delay_min, delay_max = 2.0, 5.0

                # Запуск пайплайна сессии
                status, details, check_url = await process_account_telethon(
                    session_name=session_name,
                    ref_link=ref_link,
                    channels=channels,
                    check_amount=check_amount,
                    check_count=check_count,
                    delay_min=delay_min,
                    delay_max=delay_max
                )

                # Фиксируем статус в БД
                await db.update_session_status(session_name, status, details)

                # Если чек получен
                if check_url:
                    amount_float = float(check_amount) if check_amount.replace('.', '', 1).isdigit() else 0.1
                    await db.add_check(session_name, check_url, amount_float)

                    # Моментальное уведомление администратора
                    if config.ADMIN_ID:
                        try:
                            text = (
                                f"{tg_emoji('party')} <b>Новый чек получен!</b>\n\n"
                                f"{tg_emoji('user')} <b>Сессия:</b> <code>{session_name}</code>\n"
                                f"{tg_emoji('money')} <b>Сумма:</b> <code>{check_amount}$</code>\n"
                                f"{tg_emoji('link')} <b>Чек:</b>\n{check_url}"
                            )
                            await self.bot.send_message(
                                config.ADMIN_ID,
                                text,
                                parse_mode=ParseMode.HTML,
                                disable_web_page_preview=True
                            )
                        except Exception as e:
                            logger.error(f"Ошибка отправки чека админу: {e}")

                logger.info(f"Сессия {session_name} завершена со статусом: {status} ({details})")

                # Пауза перед следующей сессией
                await asyncio.sleep(delay_max)

        except asyncio.CancelledError:
            logger.info("Фоновая задача воркера была отменена")
        except Exception as e:
            logger.error(f"Ошибка в цикле воркера: {e}", exc_info=True)
            self._is_active = False
            await db.set_setting("is_running", "0")
