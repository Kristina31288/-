"""Фоновая отправка отложенных сообщений (цепочка советов и «Как прошла неделя?»).

Без внешних сервисов: раз в минуту читает очередь scheduled_messages в SQLite.
Что и когда слать решает followup.check(); здесь — только отправка и ошибки Telegram.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
)

import followup
from config import Config
from handlers import send_outgoing
from storage import Storage, utcnow

log = logging.getLogger(__name__)

TICK_SECONDS = 60
PAUSE_BETWEEN_USERS = 0.05     # не упираться в лимиты Telegram


async def send_due(bot: Bot, db: Storage, cfg: Config, now: datetime | None = None) -> dict[str, int]:
    """Один проход по очереди. Возвращает счётчики (для журнала и тестов)."""
    now = now or utcnow()
    stats = {"sent": 0, "cancelled": 0, "rescheduled": 0, "blocked": 0, "retry": 0}
    for msg in db.due_messages(now):
        verdict = followup.check(db, msg, now)
        if verdict.action == "cancel":
            db.cancel_message(msg.id, verdict.reason)
            stats["cancelled"] += 1
            continue
        if verdict.action == "reschedule":
            db.reschedule(msg.id, verdict.due)
            stats["rescheduled"] += 1
            continue
        try:
            await send_outgoing(bot, cfg, msg.user_id, followup.build_scheduled(db, cfg, msg))
        except TelegramForbiddenError:
            # заблокировал бота: помечаем и больше ничего не шлём
            followup.on_blocked(db, msg.user_id, now)
            stats["blocked"] += 1
            continue
        except (TelegramRetryAfter, TelegramNetworkError):
            stats["retry"] += 1          # останется в очереди, попробуем через минуту
            break
        except TelegramAPIError as e:
            log.warning("Отложенное сообщение %s не отправлено: %s", msg.id, e)
            db.cancel_message(msg.id, "telegram_error")
            stats["cancelled"] += 1
            continue
        followup.after_sent(db, msg, now)
        stats["sent"] += 1
        await asyncio.sleep(PAUSE_BETWEEN_USERS)
    return stats


async def scheduler_loop(bot: Bot, db: Storage, cfg: Config, interval: float = TICK_SECONDS) -> None:
    while True:
        try:
            stats = await send_due(bot, db, cfg)
            if any(stats.values()):
                log.info("Отложенные сообщения: %s", stats)
        except Exception:
            log.exception("Ошибка планировщика отложенных сообщений")
        await asyncio.sleep(interval)
