"""Точка входа: python main.py"""
from __future__ import annotations

import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand

import safety
from config import BASE_DIR, ConfigError, load_config, load_dotenv_file
from dialog import DialogContext
from handlers import Deps, build_router
from llm import make_assistant
from scheduler import scheduler_loop
from storage import Storage

log = logging.getLogger("bot")

CRISIS_NO_SALES_SECONDS = 24 * 3600      # сутки без продаж после кризисного сообщения
OWNER_NOTIFY_COOLDOWN_SECONDS = 10 * 60  # не чаще раза в 10 минут на одного пользователя


async def purge_loop(storage: Storage, days: int) -> None:
    while True:
        try:
            n = storage.purge_old_dialogs(days)
            if n:
                log.info("Удалено старых записей журнала: %s", n)
        except Exception:
            log.exception("Ошибка очистки журнала")
        await asyncio.sleep(24 * 3600)


async def run() -> None:
    load_dotenv_file(BASE_DIR / ".env")
    try:
        config = load_config()
    except ConfigError as e:
        print(f"Ошибка настройки: {e}", file=sys.stderr)
        raise SystemExit(2)

    storage = Storage(config.db_path)
    assistant = make_assistant(config)
    log.info("ИИ-помощник: %s", "GigaChat" if config.llm_enabled else "ответы из faq.json (без ИИ)")

    dctx = DialogContext(
        assistant=assistant,
        max_len=config.max_message_len,
        limiter=safety.RateLimiter(config.rate_limit_count, config.rate_limit_window),
        crisis_cooldown=safety.Cooldown(CRISIS_NO_SALES_SECONDS),
        owner_notify_cooldown=safety.Cooldown(OWNER_NOTIFY_COOLDOWN_SECONDS),
    )

    bot = Bot(token=config.bot_token)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(build_router(Deps(config=config, storage=storage, dialog=dctx)))

    await bot.set_my_commands([
        BotCommand(command="start", description="Начать"),
        BotCommand(command="menu", description="Меню"),
        BotCommand(command="news_off", description="Отписаться от новостей"),
        BotCommand(command="stop_tips", description="Не присылать советы"),
        BotCommand(command="delete_me", description="Удалить мои данные"),
        BotCommand(command="help", description="Помощь"),
    ])

    purge_task = asyncio.create_task(purge_loop(storage, config.dialog_log_days))
    # очередь отложенных сообщений (цепочка советов, «Как прошла неделя?») — раз в минуту
    scheduler_task = asyncio.create_task(scheduler_loop(bot, storage, config))
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        purge_task.cancel()
        scheduler_task.cancel()
        storage.close()
        await bot.session.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(run())


if __name__ == "__main__":
    main()
