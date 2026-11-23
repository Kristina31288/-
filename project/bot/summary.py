"""Ежедневная сводка владелице: каждый день в 10:00 МСК, одно сообщение.

Владелица с клиентами не переписывается — всё, что требует её действий, она видит здесь:
- счётчики за сутки: новые пользователи, покупки (записанные в боте), замены, заявления
  о возврате, срабатывания кризисного сценария (только число);
- заявления о возврате без решения: №, user_id, e-mail оплаты, основание, дата подачи и
  «осталось N дн.» до 10-дневного срока рассмотрения (≤3 дня — пометка «СРОЧНО»);
  решение → /refunded <№> (возврат выполнен) или /reject <№> (отказ);
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

SUMMARY_TIME = time(10, 0)       # МСК, каждый день
PERIOD = timedelta(days=1)
REFUND_DEADLINE_DAYS = 10        # срок рассмотрения заявления о возврате (календарные дни)
URGENT_DAYS = 3                  # осталось ≤3 дней — подсветить
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
    """Последний момент «10:00 МСК», не позже now."""
    local = msk(now)
    slot = datetime.combine(local.date(), SUMMARY_TIME, MSK)
    if slot > local:
        slot -= PERIOD
    return slot


def next_slot(now: datetime) -> datetime:
    return last_slot(now) + PERIOD


def due_slot(db: Storage, now: datetime) -> datetime | None:
    """Слот, за который пора отправить сводку, или None.

    При первом запуске бота ничего не шлём: запоминаем текущий слот, первая сводка придёт
    в ближайшие 10:00 МСК. Если бот был выключен в 10:00 — сводка уйдёт после запуска
    (одна, за последние сутки).
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


_REASONS = {
    "double": "платёж прошёл дважды",
    "file": "файл не пришёл / не открывается, повторная выдача не помогла",
    "other": "другая причина (замену предлагали)",
}


def days_left(created_at: str | datetime, today: datetime) -> int:
    """Сколько календарных дней осталось до конца 10-дневного срока (0 — последний день,
    отрицательное — срок прошёл). Считаем по датам МСК от дня подачи заявления."""
    created = datetime.fromisoformat(created_at) if isinstance(created_at, str) else created_at
    deadline = msk(created).date() + timedelta(days=REFUND_DEADLINE_DAYS)
    return (deadline - msk(today).date()).days


def _deadline_text(left: int) -> str:
    if left < 0:
        text = f"СРОК ПРОШЁЛ {-left} дн. назад"
    elif left == 0:
        text = "срок истекает СЕГОДНЯ"
    else:
        text = f"осталось {left} дн. до 10-дневного срока"
    return ("СРОЧНО — " + text) if left <= URGENT_DAYS else text


def _refund_line(r: dict, end: datetime) -> str:
    reason = _REASONS.get(r.get("reason") or "", "основание не указано")
    if r.get("reason") == "double":
        reason += f" (дата платежей со слов клиента: {r.get('paid_on') or 'не указана'})"
    return (f"№{r['id']} · {_deadline_text(days_left(r['created_at'], end))} · "
            f"подано {_d(r['created_at'])} · user_id {r['user_id']} · e-mail оплаты {r['email']} · "
            f"{reason} · покупка {_d(r['purchased_at'])} · {_product(r['product'])}")


def build(db: Storage, end: datetime) -> str:
    """Сводка за сутки до end (не включая end). Сроки по заявлениям — на дату end."""
    start = end - PERIOD
    c = {k: db.count_between(k, start, end) for k in ("users", "purchases", "swaps", "refunds", "crisis")}
    lines = [
        f"Сводка за сутки {msk(start).strftime('%d.%m %H:%M')}–{msk(end).strftime('%d.%m.%Y %H:%M')} (МСК)",
        "",
        f"Новых пользователей: {c['users']}",
        f"Покупок (записанных в боте): {c['purchases']}",
        f"Замен разбора (и подарков вместо замены набора): {c['swaps']}",
        f"Новых заявлений о возврате: {c['refunds']}",
    ]
    if c["crisis"]:
        lines.append(f"Срабатываний кризисного сценария: {c['crisis']} (бот ответил фиксированным текстом)")

    refunds = sorted(db.refund_requests(open_only=True),
                     key=lambda r: (days_left(r["created_at"], end), r["id"]))
    lines += ["", f"Заявления о возврате без решения: {len(refunds)}"]
    if refunds:
        urgent = sum(1 for r in refunds if days_left(r["created_at"], end) <= URGENT_DAYS)
        if urgent:
            lines.append(f"СРОЧНО (осталось 3 дня или меньше): {urgent}")
        lines.append("Возврат выполнен в кабинете платёжного сервиса → /refunded <№>; "
                     "отказ → /reject <№> (добавьте notify, чтобы бот сообщил клиенту решение).")
        for r in refunds:
            lines.append(_refund_line(r, end))

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
