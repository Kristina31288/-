"""Логика обработки свободного текста пользователя — без aiogram, тестируется отдельно.

Порядок проверок важен:
1. Кризис — ВСЕГДА первым, по полному тексту, даже если сообщение длинное или
   пользователь превысил лимит. Ответ фиксированный, без продаж, владелица уведомляется.
2. Длина сообщения.
3. Антиспам.
4. Просьба о человеке / жалоба / возврат / злость → передача владелице.
5. Ответ ИИ-помощника (или FAQ).
"""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from enum import Enum

import safety
from llm import Assistant


class Kind(str, Enum):
    CRISIS = "crisis"
    TOO_LONG = "too_long"
    RATE_LIMITED = "rate_limited"
    HANDOFF = "handoff"
    ANSWER = "answer"


@dataclass(frozen=True)
class Decision:
    kind: Kind
    reply: str
    notify_owner: bool = False
    owner_note: str = ""            # причина для уведомления владелицы
    allow_sales: bool = False       # можно ли показывать кнопки оплаты
    log_user_text: bool = True      # сохранять ли текст пользователя в журнал


@dataclass
class DialogContext:
    assistant: Assistant
    max_len: int
    limiter: safety.RateLimiter
    crisis_cooldown: safety.Cooldown        # после кризиса: не продавать
    owner_notify_cooldown: safety.Cooldown  # не спамить владелицу одинаковыми уведомлениями
    history: dict[int, deque] = field(default_factory=lambda: defaultdict(lambda: deque(maxlen=6)))

    def forget(self, user_id: int) -> None:
        self.limiter.forget(user_id)
        self.crisis_cooldown.forget(user_id)
        self.owner_notify_cooldown.forget(user_id)
        self.history.pop(user_id, None)


async def process_text(ctx: DialogContext, user_id: int, text: str) -> Decision:
    text = text or ""

    crisis = safety.detect_crisis(text)
    if crisis.is_crisis:
        ctx.crisis_cooldown.mark(user_id)
        ctx.history.pop(user_id, None)
        notify = not ctx.owner_notify_cooldown.active(user_id)
        if notify:
            ctx.owner_notify_cooldown.mark(user_id)
        return Decision(Kind.CRISIS, safety.CRISIS_REPLY, notify_owner=notify,
                        owner_note=f"кризис ({crisis.category})", allow_sales=False,
                        log_user_text=False)

    if safety.is_too_long(text, ctx.max_len):
        return Decision(Kind.TOO_LONG, safety.TOO_LONG_REPLY.format(limit=ctx.max_len),
                        log_user_text=False)

    if not ctx.limiter.allow(user_id):
        return Decision(Kind.RATE_LIMITED, safety.RATE_LIMITED_REPLY, log_user_text=False)

    handoff = safety.detect_handoff(text)
    if handoff.needed:
        return Decision(Kind.HANDOFF, safety.HANDOFF_REPLY, notify_owner=True,
                        owner_note=f"передача ({handoff.reason})")

    no_sales = ctx.crisis_cooldown.active(user_id)
    history = list(ctx.history[user_id])
    reply = await ctx.assistant.answer(text, history, no_sales=no_sales)
    ctx.history[user_id].append(("user", text))
    ctx.history[user_id].append(("bot", reply.text))
    if reply.handoff:
        return Decision(Kind.HANDOFF, reply.text + "\n\n" + safety.HANDOFF_REPLY,
                        notify_owner=True, owner_note="вопрос вне базы знаний / передача от ИИ",
                        allow_sales=False)
    return Decision(Kind.ANSWER, reply.text, allow_sales=not no_sales)
