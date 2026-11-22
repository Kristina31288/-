"""Еженедельная сводка владелице: раз в неделю, понедельник 10:00 МСК, одно сообщение.

Владелица с клиентами не переписывается — всё, что требует её действий, она видит здесь:
- счётчики за неделю: новые пользователи, покупки (записанные в боте), замены, заявки на
  возврат, срабатывания кризисного сценария (только число);
- невыполненные заявки на возврат: user_id, e-mail оплаты, дата покупки, продукт →
  возврат в кабинете платёжного сервиса, затем /refunded <user_id>;
- кому выдать файл после проверки оплаты (нет записи о покупке в боте) → /give;
- вопросы, на которые Виктория не нашла ответа в базе — БЕЗ user_id и без контактов.

Персональных данных сверх нужного для действия нет: ни имён, ни текстов переписки.
Только стандартная библиотека.
"""
from __future__ import annotations

import re
from datetime import datetime, time, timedelta

import catalog
from followup import MSK, SHORT_TITLES, msk
from storage import Storage

SUMMARY_WEEKDAY = 0              # понедельник
SUMMARY_TIME = time(10, 0)       # МСК
PERIOD = timedelta(days=7)
META_KEY = "summary_last_slot"
MAX_QUESTIONS = 15
MAX_QUESTION_LEN = 200
MAX_MESSAGE_LEN = 4000           # лимит Telegram 4096

_EMAIL_RE = re.compile(r"[\w.%+\-]+@[\w\-]+(?:\.[\w\-]+)+")
_URL_RE = re.compile(r"(?:https?://|www\.|t\.me/)\S+", re.IGNORECASE)
_PHONE_RE = re.compile(r"(?<!\w)\+?\d[\d\s\-()]{8,}\d")
_MENTION_RE = re.compile(r"@\w{3,}")


def scrub(text: str, limit: int = MAX_QUESTION_LEN) -> str:
    """Вырезает e-mail, телефоны, ссылки и @username; укорачивает. Для вопросов в сводку."""
    t = _EMAIL_RE.sub("[e-mail]", text or "")
    t = _URL_RE.sub("[ссылка]", t)
    t = _PHONE_RE.sub("[телефон]", t)
    t = _MENTION_RE.sub("[ник]", t)
    t = " ".join(t.split())
    return t if len(t) <= limit else t[: limit - 1].rstrip() + "…"


# --- расписание -----------------------------------------------------------
def last_slot(now: datetime) -> datetime:
    """Последний момент «понедельник 10:00 МСК», не позже now."""
    local = msk(now)
    days_back = (local.weekday() - SUMMARY_WEEKDAY) % 7
    slot = datetime.combine(local.date() - timedelta(days=days_back), SUMMARY_TIME, MSK)
    if slot > local:
        slot -= PERIOD
    return slot


def next_slot(now: datetime) -> datetime:
    return last_slot(now) + PERIOD


def due_slot(db: Storage, now: datetime) -> datetime | None:
    """Слот, за который пора отправить сводку, или None.

    При первом запуске бота ничего не шлём: запоминаем текущий слот, первая сводка придёт
    в ближайший понедельник. Если бот был выключен в понедельник — сводка уйдёт после запуска.
    """
    slot = last_slot(now)
    sent = db.get_meta(META_KEY)
    if sent is None:
        db.set_meta(META_KEY, slot.isoformat())
        return None
    return slot if datetime.fromisoformat(sent) < slot else None


def mark_sent(db: Storage, slot: datetime) -> None:
    db.set_meta(META_KEY, slot.isoformat())


# --- текст ------------------------------------------------------------------
def _d(value: str | datetime | None) -> str:
    if not value:
        return "—"
    dt = datetime.fromisoformat(value) if isinstance(value, str) else value
    return msk(dt).strftime("%d.%m.%Y")


def _product(key: str | None) -> str:
    if not key:
        return "нет записи в боте — найдите платёж по e-mail"
    if key in SHORT_TITLES:
        return SHORT_TITLES[key]
    p = catalog.PRODUCTS.get(key)
    return p.title if p else key


def build(db: Storage, end: datetime) -> str:
    """Сводка за 7 дней до end (не включая end)."""
    start = end - PERIOD
    c = {k: db.count_between(k, start, end) for k in ("users", "purchases", "swaps", "refunds", "crisis")}
    lines = [
        f"Сводка за неделю {_d(start)}–{_d(end - timedelta(seconds=1))} (МСК)",
        "",
        f"Новых пользователей: {c['users']}",
        f"Покупок (записанных в боте): {c['purchases']}",
        f"Замен разбора (и подарков вместо замены набора): {c['swaps']}",
        f"Заявок на возврат за неделю: {c['refunds']}",
    ]
    if c["crisis"]:
        lines.append(f"Срабатываний кризисного сценария: {c['crisis']} (бот ответил фиксированным текстом)")

    refunds = db.refund_requests(open_only=True)
    lines += ["", f"Возвраты к оформлению: {len(refunds)}"]
    if refunds:
        lines.append("Сделайте возврат в кабинете платёжного сервиса, затем /refunded <user_id>.")
        for i, r in enumerate(refunds, 1):
            lines.append(f"{i}. user_id {r['user_id']} · e-mail оплаты {r['email']} · "
                         f"покупка {_d(r['purchased_at'])} · {_product(r['product'])} · "
                         f"заявка {_d(r['created_at'])}")

    deliveries = db.delivery_requests(open_only=True)
    if deliveries:
        lines += ["", f"Выдать файл после проверки оплаты: {len(deliveries)}",
                  "Найдите платёж по e-mail и выдайте: /give <user_id> <продукт>."]
        for i, r in enumerate(deliveries, 1):
            what = "файл не пришёл" if r["kind"] == "file" else \
                f"замена → {_product(r['product']) if r['product'] else 'разбор не указан'}"
            lines.append(f"{i}. user_id {r['user_id']} · e-mail оплаты {r['email']} · {what} · "
                         f"{_d(r['created_at'])}")

    questions = db.unanswered_between(start, end)
    lines += ["", f"Вопросы без ответа в базе: {len(questions)}"]
    if questions:
        lines.append("Добавьте ответы в knowledge.md и faq.json.")
        for q in questions[:MAX_QUESTIONS]:
            lines.append(f"- «{scrub(q)}»")
        if len(questions) > MAX_QUESTIONS:
            lines.append(f"…и ещё {len(questions) - MAX_QUESTIONS}")
    text = "\n".join(lines)
    if len(text) > MAX_MESSAGE_LEN:
        text = text[: MAX_MESSAGE_LEN - 1] + "…"
    return text
