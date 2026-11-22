"""Автоцепочка после «10 фраз» и «Как прошла неделя?» после покупки.

Только стандартная библиотека: расписание, проверки и сборка сообщений тестируются
без aiogram. Отправку делает scheduler.py, кнопки обрабатывает handlers.py.

Цепочка (только с согласием на ПДн И на рассылку — это рекламные сообщения, 38-ФЗ ст. 18):
  tip1 — день 1 после «10 фраз», 19:00–21:00 МСК: какую фразу начать;
  tip2 — день 2: вымышленный пример + фраза из платного разбора;
  tip3 — день 3: «Какая ситуация у вас сейчас?» + три кнопки → карточка разбора + набор;
  tip3_repeat — единственный мягкий повтор через 2 дня после tip3, если покупки нет.
  После покупки оставшиеся сообщения цепочки отменяются: больше не продаём.
week — через 3 дня после /give (не в тихие часы, перенос на 10:00).

Безопасность: кризисная отметка → все продающие сообщения отменяются на 7 дней и не
отправляются; отписка (/stop_tips), /delete_me и блокировка бота — тоже.
no_sell (после заявки на возврат) → цепочка и «Как прошла неделя?» не отправляются никогда,
карточки разборов — без кнопок оплаты.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone

import catalog
import texts
from config import Config
from storage import Scheduled, Storage, utcnow

MSK = timezone(timedelta(hours=3), "MSK")      # в Москве нет перехода на летнее время
CHAIN_WINDOW_START = time(19, 0)
CHAIN_WINDOW_END = time(21, 0)
CHAIN_WINDOW_MINUTES = 120
QUIET_START = time(22, 0)
QUIET_END = time(9, 0)
WEEK_SEND_AT = time(10, 0)
CRISIS_PAUSE = timedelta(days=7)
WEEK_AFTER = timedelta(days=3)
REPEAT_AFTER_DAYS = 2

TIP1, TIP2, TIP3, TIP3_REPEAT, WEEK = "tip1", "tip2", "tip3", "tip3_repeat", "week"
CHAIN_DAYS = {TIP1: 1, TIP2: 2, TIP3: 3}
CHAIN_KINDS = (TIP1, TIP2, TIP3, TIP3_REPEAT)
# Продающими считаем все: даже совет из tip1 после кризиса неуместен.
SALES_KINDS = CHAIN_KINDS + (WEEK,)

GUIDE_KEYS = ("telefon", "podrostok", "uroki")
SITUATION_BUTTONS = {
    "telefon": texts.BTN_SIT_TELEFON,
    "podrostok": texts.BTN_SIT_PODROSTOK,
    "uroki": texts.BTN_SIT_UROKI,
}
SHORT_TITLES = {"telefon": "Телефон и игры", "podrostok": "Подросток хамит или закрылся",
                "uroki": "Уроки без крика"}
WEEK_ANSWERS = ("calm", "hard", "nofit")


# --- время ------------------------------------------------------------------
def msk(dt: datetime) -> datetime:
    return dt.astimezone(MSK)


def _user_offset(user_id: int) -> timedelta:
    """Разносим отправку по окну 19:00–20:59, чтобы не слать всем в 19:00 ровно."""
    return timedelta(minutes=user_id % CHAIN_WINDOW_MINUTES)


def chain_slot(day: date, user_id: int) -> datetime:
    return datetime.combine(day, CHAIN_WINDOW_START, MSK) + _user_offset(user_id)


def in_chain_window(now: datetime) -> bool:
    t = msk(now).time()
    return CHAIN_WINDOW_START <= t < CHAIN_WINDOW_END


def is_quiet(now: datetime) -> bool:
    t = msk(now).time()
    return t >= QUIET_START or t < QUIET_END


def next_chain_time(now: datetime, user_id: int) -> datetime:
    """Ближайший момент в окне 19:00–21:00 МСК (сейчас, если окно уже идёт)."""
    if in_chain_window(now):
        return now
    local = msk(now)
    day = local.date() if local.time() < CHAIN_WINDOW_START else local.date() + timedelta(days=1)
    return chain_slot(day, user_id)


def next_daytime(now: datetime) -> datetime:
    """Вне тихих часов — сразу; в тихие часы (22:00–9:00 МСК) — ближайшие 10:00."""
    if not is_quiet(now):
        return now
    local = msk(now)
    day = local.date() + timedelta(days=1) if local.time() >= QUIET_START else local.date()
    return datetime.combine(day, WEEK_SEND_AT, MSK)


def plan_chain(user_id: int, start: datetime) -> list[tuple[str, datetime]]:
    day0 = msk(start).date()
    return [(kind, chain_slot(day0 + timedelta(days=d), user_id)) for kind, d in CHAIN_DAYS.items()]


def plan_week(given_at: datetime) -> datetime:
    return next_daytime(given_at + WEEK_AFTER)


# --- события ----------------------------------------------------------------
def start_chain(db: Storage, user_id: int, now: datetime | None = None) -> int:
    """После выдачи «10 фраз». Каждое сообщение цепочки ставится не больше одного раза."""
    now = now or utcnow()
    return sum(db.schedule_message(user_id, kind, due, once=True, now=now)
               for kind, due in plan_chain(user_id, now))


def on_purchase(db: Storage, user_id: int, purchase_id: int | None,
                now: datetime | None = None) -> None:
    """Купил — цепочке больше не продаём; ставим «Как прошла неделя?»."""
    now = now or utcnow()
    db.cancel_pending(user_id, CHAIN_KINDS, reason="purchased")
    db.cancel_pending(user_id, (WEEK,), reason="replaced_by_new_purchase")
    db.schedule_message(user_id, WEEK, plan_week(now), once=False, ref=purchase_id, now=now)


def stop_tips(db: Storage, user_id: int, now: datetime | None = None) -> int:
    db.set_tips_off(user_id, now)
    return db.cancel_pending(user_id, SALES_KINDS, reason="unsubscribed")


def on_crisis(db: Storage, user_id: int, now: datetime | None = None) -> int:
    """Кризисная отметка: продающие сообщения на ближайшие 7 дней отменяются.
    Те, что позже, тоже не уйдут, пока отметке нет 7 дней (проверка при отправке)."""
    now = now or utcnow()
    if db.get_user(user_id) is None:
        return 0           # без согласия ничего не храним и цепочки нет
    db.mark_crisis(user_id, now)
    return db.cancel_pending(user_id, SALES_KINDS, reason="crisis", before=now + CRISIS_PAUSE)


def on_blocked(db: Storage, user_id: int, now: datetime | None = None) -> int:
    db.mark_blocked(user_id, now)
    return db.cancel_pending(user_id, reason="blocked")


def sales_paused(db: Storage, user_id: int, now: datetime | None = None) -> bool:
    now = now or utcnow()
    crisis_at = db.followup_state(user_id).crisis_at
    return crisis_at is not None and now - crisis_at < CRISIS_PAUSE


def owned_guides(db: Storage, user_id: int) -> set[str]:
    owned: set[str] = set()
    for p in db.list_purchases(user_id):
        product = catalog.PRODUCTS.get(p.product)
        if product:
            owned.update(product.bundle or (product.key,))
    return owned


# --- проверка перед отправкой -----------------------------------------------
@dataclass(frozen=True)
class Verdict:
    action: str                    # send | cancel | reschedule
    reason: str = ""
    due: datetime | None = None


SEND = Verdict("send")


def check(db: Storage, msg: Scheduled, now: datetime | None = None) -> Verdict:
    now = now or utcnow()
    user = db.get_user(msg.user_id)
    if user is None or not user.has_consent:
        return Verdict("cancel", "no_user")
    state = db.followup_state(msg.user_id)
    if state.blocked:
        return Verdict("cancel", "blocked")
    if state.tips_off:
        return Verdict("cancel", "unsubscribed")
    if msg.kind in SALES_KINDS and db.is_no_sell(msg.user_id):
        return Verdict("cancel", "no_sell")
    if msg.kind in SALES_KINDS and sales_paused(db, msg.user_id, now):
        return Verdict("cancel", "crisis")
    if msg.kind in CHAIN_KINDS:
        if not user.marketing_consent:
            return Verdict("cancel", "no_marketing_consent")
        if db.list_purchases(msg.user_id):
            return Verdict("cancel", "purchased")
        if not in_chain_window(now):
            return Verdict("reschedule", "window", next_chain_time(now, msg.user_id))
    elif msg.kind == WEEK:
        if not db.list_purchases(msg.user_id):
            return Verdict("cancel", "no_purchase")
        if is_quiet(now):
            return Verdict("reschedule", "quiet_hours", next_daytime(now))
    else:
        return Verdict("cancel", "unknown_kind")
    return SEND


def after_sent(db: Storage, msg: Scheduled, now: datetime | None = None) -> None:
    """После tip3 — один мягкий повтор. После повтора ничего не ставим."""
    now = now or utcnow()
    db.mark_sent(msg.id, now)
    if msg.kind == TIP3:
        day = msk(now).date() + timedelta(days=REPEAT_AFTER_DAYS)
        db.schedule_message(msg.user_id, TIP3_REPEAT, chain_slot(day, msg.user_id),
                            once=True, now=now)


# --- сообщения ----------------------------------------------------------------
@dataclass(frozen=True)
class Button:
    text: str
    callback: str | None = None
    url: str | None = None


@dataclass(frozen=True)
class Outgoing:
    text: str
    rows: list[list[Button]] = field(default_factory=list)
    send_lead_magnet: bool = False      # перед текстом приложить PDF «10 фраз»


STOP_ROW = [Button(texts.BTN_STOP_TIPS, callback="fu:stop")]


def situation_rows() -> list[list[Button]]:
    return [[Button(SITUATION_BUTTONS[k], callback=f"fu:sit:{k}")] for k in GUIDE_KEYS]


def pay_row(cfg: Config, key: str) -> list[list[Button]]:
    link = cfg.pay_links.get(key, "")
    product = catalog.PRODUCTS[key]
    if not link:
        return []
    return [[Button(texts.PAY_BUTTON.format(price=product.price_text), url=link)]]


def _with_pay(cfg: Config, key: str, text: str) -> Outgoing:
    rows = pay_row(cfg, key)
    return Outgoing(text if rows else text + "\n\n" + texts.PAY_LINK_MISSING, rows)


def build_card(cfg: Config, key: str, owned: set[str], sell: bool = True) -> list[Outgoing]:
    """Карточка разбора по кнопке ситуации и под ней — предложение набора.
    sell=False (no_sell): только описание, без цены, кнопок оплаты и набора."""
    p = catalog.PRODUCTS[key]
    if not sell:
        return [Outgoing(f"{p.title}\n\n{p.description}")]
    if key in owned:
        out = [Outgoing(texts.CARD_OWNED.format(title=p.title))]
    else:
        out = [_with_pay(cfg, key, texts.CARD.format(title=p.title, description=p.description,
                                                     price=p.price_text))]
    if not set(GUIDE_KEYS) <= owned:
        nabor = catalog.PRODUCTS["nabor"]
        out.append(_with_pay(cfg, "nabor", texts.NABOR_OFFER.format(price=nabor.price_text)))
    return out


def build_scheduled(db: Storage, cfg: Config, msg: Scheduled) -> list[Outgoing]:
    if msg.kind == TIP1:
        return [Outgoing(texts.TIP1, [STOP_ROW])]
    if msg.kind == TIP2:
        return [Outgoing(texts.TIP2, [STOP_ROW])]
    if msg.kind == TIP3:
        return [Outgoing(texts.TIP3, situation_rows() + [STOP_ROW])]
    if msg.kind == TIP3_REPEAT:
        chosen = db.followup_state(msg.user_id).chosen_product
        if chosen in GUIDE_KEYS:
            p = catalog.PRODUCTS[chosen]
            o = _with_pay(cfg, chosen, texts.TIP3_REPEAT_CHOSEN.format(
                short=SHORT_TITLES[chosen], price=p.price_text))
            return [Outgoing(o.text, o.rows + [STOP_ROW])]
        return [Outgoing(texts.TIP3_REPEAT, situation_rows() + [STOP_ROW])]
    if msg.kind == WEEK:
        ref = msg.ref or 0
        return [Outgoing(texts.WEEK, [
            [Button(texts.BTN_WEEK_CALM, callback=f"fu:wk:calm:{ref}")],
            [Button(texts.BTN_WEEK_HARD, callback=f"fu:wk:hard:{ref}")],
            [Button(texts.BTN_WEEK_NOFIT, callback=f"fu:wk:nofit:{ref}")],
        ])]
    raise ValueError(f"Неизвестный вид сообщения: {msg.kind}")


def _guide_offers(cfg: Config, keys: list[str]) -> list[list[Button]]:
    rows: list[list[Button]] = []
    for k in keys:
        link = cfg.pay_links.get(k, "")
        if link:
            rows.append([Button(f"{SHORT_TITLES[k]} — {catalog.PRODUCTS[k].price_text}", url=link)])
    return rows


def build_week_answer(db: Storage, cfg: Config, user_id: int, answer: str,
                      purchase_id: int, sell: bool = True) -> list[Outgoing]:
    if not sell:   # no_sell: ни одного предложения, замены и подарка (по покупке оформлен возврат)
        if answer == "calm":
            return [Outgoing(texts.WEEK_CALM)]
        if answer == "hard":
            return [Outgoing(texts.WEEK_HARD, send_lead_magnet=True), Outgoing(texts.WEEK_HARD_SAFETY)]
        return [Outgoing(texts.WEEK_NOFIT_NO_SELL)]
    owned = owned_guides(db, user_id)
    others = [k for k in GUIDE_KEYS if k not in owned]
    price = catalog.format_price(catalog.PRODUCTS["telefon"].price_rub)
    if answer == "calm":
        if not others:
            return [Outgoing(texts.WEEK_CALM + " " + texts.WEEK_CALM_ALL)]
        if len(owned) == 1:
            upgrade = cfg.upgrade_link or texts.UPGRADE_PLACEHOLDER
            text = texts.WEEK_CALM + "\n\n" + texts.WEEK_CALM_UPGRADE.format(upgrade=upgrade,
                                                                              price=price)
        else:
            text = texts.WEEK_CALM + "\n\n" + texts.WEEK_CALM_OTHER.format(price=price)
        rows = _guide_offers(cfg, others)
        return [Outgoing(text if rows else text + "\n\n" + texts.PAY_LINK_MISSING, rows)]
    if answer == "hard":
        out = [Outgoing(texts.WEEK_HARD, send_lead_magnet=True)]
        if others:
            if len(owned) == 1:
                upgrade = cfg.upgrade_link or texts.UPGRADE_PLACEHOLDER
                text = texts.WEEK_HARD_UPGRADE.format(upgrade=upgrade, price=price)
            else:
                text = texts.WEEK_HARD_MORE.format(price=price)
            rows = _guide_offers(cfg, others)
            out.append(Outgoing(text if rows else text + "\n\n" + texts.PAY_LINK_MISSING, rows))
        out.append(Outgoing(texts.WEEK_HARD_SAFETY))
        return out
    if answer == "nofit":
        purchase = db.get_purchase(purchase_id) if purchase_id else None
        if purchase is None or purchase.user_id != user_id:
            return [Outgoing(texts.WEEK_NOFIT_NOTHING)]
        if purchase.product == "nabor":
            if gift_used(db, user_id, purchase_id):
                return [Outgoing(texts.WEEK_NOFIT_NABOR.rsplit(" А ещё", 1)[0])]
            return [Outgoing(texts.WEEK_NOFIT_NABOR, gift_rows(purchase_id))]
        if swap_used(db, user_id, purchase_id):
            return [Outgoing(texts.SWAP_ALREADY)]
        choices = [k for k in GUIDE_KEYS if k not in owned]
        if not choices:
            return [Outgoing(texts.WEEK_NOFIT_NOTHING)]
        rows = [[Button(SHORT_TITLES[k], callback=f"fu:swap:{purchase_id}:{k}")] for k in choices]
        return [Outgoing(texts.WEEK_NOFIT_SWAP, rows)]
    raise ValueError(answer)


# --- замена разбора («Не подошло») --------------------------------------------
SWAP_SOURCE = "swap"
GIFT_SOURCE = "gift"          # подарок вместо замены для набора («Карта спокойного вечера»)
NOT_PAID_SOURCES = (SWAP_SOURCE, GIFT_SOURCE)


def gift_rows(purchase_id: int) -> list[list[Button]]:
    return [[Button(texts.BTN_GIFT, callback=f"fu:gift:{purchase_id}")]]


def gift_used(db: Storage, user_id: int, purchase_id: int) -> bool:
    return any(p.source == GIFT_SOURCE and p.external_id == f"gift-{purchase_id}"
               for p in db.list_purchases(user_id))


def check_gift(db: Storage, user_id: int, purchase_id: int) -> SwapCheck:
    purchase = db.get_purchase(purchase_id)
    if purchase is None or purchase.user_id != user_id or purchase.product != "nabor":
        return SwapCheck(False, texts.WEEK_NOFIT_NOTHING)
    if gift_used(db, user_id, purchase_id):
        return SwapCheck(False, texts.GIFT_ALREADY)
    return SwapCheck(True)


def record_gift(db: Storage, user_id: int, purchase_id: int, now: datetime | None = None) -> bool:
    """Один подарок на покупку набора (уникальный индекс source + external_id)."""
    return db.add_purchase(user_id, catalog.GIFT_EVENING_KEY, GIFT_SOURCE, f"gift-{purchase_id}", now=now)


def swap_used(db: Storage, user_id: int, purchase_id: int) -> bool:
    return any(p.source == SWAP_SOURCE and p.external_id == f"swap-{purchase_id}"
               for p in db.list_purchases(user_id))


@dataclass(frozen=True)
class SwapCheck:
    ok: bool
    reply: str = ""


def check_swap(db: Storage, user_id: int, purchase_id: int, key: str) -> SwapCheck:
    purchase = db.get_purchase(purchase_id)
    if purchase is None or purchase.user_id != user_id or purchase.product == "nabor" \
            or purchase.source in NOT_PAID_SOURCES or key not in GUIDE_KEYS:
        return SwapCheck(False, texts.WEEK_NOFIT_NOTHING)
    if swap_used(db, user_id, purchase_id):
        return SwapCheck(False, texts.SWAP_ALREADY)
    if key in owned_guides(db, user_id):
        return SwapCheck(False, texts.SWAP_PRODUCT_OWNED)
    return SwapCheck(True)


def record_swap(db: Storage, user_id: int, purchase_id: int, key: str,
                now: datetime | None = None) -> bool:
    """Одна замена на покупку: уникальный индекс (source, external_id) не даст вторую."""
    return db.add_purchase(user_id, key, SWAP_SOURCE, f"swap-{purchase_id}", now=now)
