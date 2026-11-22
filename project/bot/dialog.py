"""Логика обработки свободного текста пользователя — без aiogram, тестируется отдельно.

Порядок проверок важен:
1. Кризис — ВСЕГДА первым, по полному тексту, даже если сообщение длинное или
   пользователь превысил лимит. Ответ фиксированный (safety.CRISIS_REPLY), без продаж.
   Владелица уведомляется, только если включён NOTIFY_OWNER_ON_CRISIS (по умолчанию нет).
2. Длина сообщения.
3. Антиспам.
4. Сервисные сценарии Виктории (support.py): возврат, файл не пришёл, «не подошло»,
   жалоба, мат, просьба о человеке. Владелице ничего не передаётся «вживую».
5. Ответ ИИ-помощницы (GigaChat) или FAQ. Нет ответа в базе → вопрос (без user_id и
   контактов) пишется в еженедельную сводку.

Режим «не продавать» (no_sales): после кризиса (сутки) и навсегда после заявки на возврат
(no_sell). Тогда ИИ получает запрет продавать, FAQ отдаёт ответы без цен и оплаты, а ответ,
похожий на продажу, заменяется нейтральным.
"""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from enum import Enum

import safety
import summary
import texts
from llm import Assistant, looks_like_sales
from storage import Storage
from support import Support


class Kind(str, Enum):
    CRISIS = "crisis"
    TOO_LONG = "too_long"
    RATE_LIMITED = "rate_limited"
    SERVICE = "service"             # сценарий support.py (возврат, файл, замена, жалоба…)
    ANSWER = "answer"


@dataclass(frozen=True)
class Decision:
    kind: Kind
    reply: str
    notify_owner: bool = False
    owner_note: str = ""            # причина для уведомления владелицы (только кризис по флагу)
    allow_sales: bool = False       # можно ли показывать кнопки оплаты
    log_user_text: bool = True      # сохранять ли текст пользователя в журнал
    rows: list = field(default_factory=list)   # инлайн-кнопки (followup.Button): замена, подарок
    resend_files: bool = False      # прислать заново PDF всех покупок
    unanswered: str = ""            # вопрос без ответа (очищенный) — записан в сводку


@dataclass
class DialogContext:
    assistant: Assistant
    max_len: int
    limiter: safety.RateLimiter
    crisis_cooldown: safety.Cooldown        # после кризиса: не продавать
    owner_notify_cooldown: safety.Cooldown  # не спамить владелицу одинаковыми уведомлениями
    storage: Storage | None = None
    notify_owner_on_crisis: bool = False    # NOTIFY_OWNER_ON_CRISIS
    history: dict[int, deque] = field(default_factory=lambda: defaultdict(lambda: deque(maxlen=6)))
    introduced: set[int] = field(default_factory=set)   # кому Виктория уже представилась
    support: Support | None = None

    def __post_init__(self) -> None:
        if self.support is None:
            self.support = Support(self.storage)

    def forget(self, user_id: int) -> None:
        self.limiter.forget(user_id)
        self.crisis_cooldown.forget(user_id)
        self.owner_notify_cooldown.forget(user_id)
        self.history.pop(user_id, None)
        self.introduced.discard(user_id)
        self.support.forget(user_id)

    def no_sell(self, user_id: int) -> bool:
        return self.storage is not None and self.storage.is_no_sell(user_id)

    def sales_blocked(self, user_id: int) -> bool:
        return self.no_sell(user_id) or self.crisis_cooldown.active(user_id)


def _introduce(ctx: DialogContext, user_id: int, text: str) -> str:
    """В первом ответе диалога Виктория честно представляется (если ещё не сделала этого)."""
    if user_id in ctx.introduced:
        return text
    ctx.introduced.add(user_id)
    if texts.VICTORIA_INTRO in text:
        return text
    return texts.victoria_intro() + "\n\n" + text


async def process_text(ctx: DialogContext, user_id: int, text: str) -> Decision:
    text = text or ""

    crisis = safety.detect_crisis(text)
    if crisis.is_crisis:
        ctx.crisis_cooldown.mark(user_id)
        ctx.history.pop(user_id, None)
        ctx.support.forget(user_id)
        notify = ctx.notify_owner_on_crisis and not ctx.owner_notify_cooldown.active(user_id)
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

    no_sell = ctx.no_sell(user_id)
    no_sales = no_sell or ctx.crisis_cooldown.active(user_id)

    service = ctx.support.handle(user_id, text, no_sell=no_sell)
    if service is not None:
        return Decision(Kind.SERVICE, _introduce(ctx, user_id, service.text), rows=service.rows,
                        resend_files=service.resend_files)

    history = list(ctx.history[user_id])
    reply = await ctx.assistant.answer(text, history, no_sales=no_sales)
    if reply.refund:                 # ИИ распознал просьбу о возврате, которую не поймали шаблоны
        service = ctx.support.start_refund(user_id, text, no_sell=no_sell)
        return Decision(Kind.SERVICE, _introduce(ctx, user_id, service.text), rows=service.rows)

    answer = reply.text
    if no_sales and looks_like_sales(answer):
        answer = texts.NO_SELL_ANSWER
    ctx.history[user_id].append(("user", text))
    ctx.history[user_id].append(("bot", answer))

    unanswered = ""
    if reply.unknown:
        unanswered = summary.scrub(text)
        if ctx.storage is not None and unanswered:
            ctx.storage.add_unanswered(unanswered)
    return Decision(Kind.ANSWER, _introduce(ctx, user_id, answer), allow_sales=not no_sales,
                    unanswered=unanswered)
