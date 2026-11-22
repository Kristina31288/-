"""Конвейер обработки текста: кризис → длина → антиспам → сценарии Виктории → ответ."""
import asyncio

import pytest

import followup
import safety
import texts
from dialog import DialogContext, Kind, process_text
from faq import FaqBase
from llm import AssistantReply, FaqAssistant
from storage import Storage


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


class SpyAssistant:
    """Запоминает вызовы; по умолчанию «продаёт»."""

    def __init__(self, reply="Разбор 1 стоит 299 ₽", unknown=False, refund=False):
        self.calls = []
        self.reply = AssistantReply(reply, unknown=unknown, refund=refund)

    async def answer(self, text, history=(), no_sales=False):
        self.calls.append((text, no_sales))
        return self.reply


def make_ctx(assistant=None, limit=5, clock=None, storage=None, notify=False):
    clock = clock or FakeClock()
    return DialogContext(
        assistant=assistant or SpyAssistant(),
        max_len=200,
        limiter=safety.RateLimiter(limit, 60, clock=clock),
        crisis_cooldown=safety.Cooldown(3600, clock=clock),
        owner_notify_cooldown=safety.Cooldown(600, clock=clock),
        storage=storage,
        notify_owner_on_crisis=notify,
    )


def run(coro):
    return asyncio.run(coro)


def test_crisis_does_not_reach_assistant_and_does_not_sell():
    spy = SpyAssistant()
    ctx = make_ctx(spy)
    d = run(process_text(ctx, 1, "Дочь сказала, что не хочет жить"))
    assert d.kind is Kind.CRISIS
    assert d.reply == safety.CRISIS_REPLY            # фиксированный текст, без представления
    assert not d.allow_sales and not d.log_user_text
    assert not d.notify_owner                        # NOTIFY_OWNER_ON_CRISIS по умолчанию выключен
    assert spy.calls == []


def test_crisis_notifies_owner_only_with_flag():
    d = run(process_text(make_ctx(notify=True), 1, "хочу умереть"))
    assert d.kind is Kind.CRISIS and d.notify_owner and d.reply == safety.CRISIS_REPLY


def test_after_crisis_assistant_is_told_not_to_sell():
    spy = SpyAssistant()
    ctx = make_ctx(spy)
    run(process_text(ctx, 1, "хочу умереть"))
    d = run(process_text(ctx, 1, "сколько стоит разбор?"))
    assert d.kind is Kind.ANSWER
    assert not d.allow_sales
    assert spy.calls[-1] == ("сколько стоит разбор?", True)


def test_crisis_checked_even_for_long_and_rate_limited():
    ctx = make_ctx(limit=1)
    run(process_text(ctx, 1, "привет"))                     # исчерпали лимит
    long_text = "бла " * 100 + " не хочу жить"
    d = run(process_text(ctx, 1, long_text))
    assert d.kind is Kind.CRISIS


def test_owner_crisis_notifications_are_throttled():
    clock = FakeClock()
    ctx = make_ctx(clock=clock, notify=True)
    assert run(process_text(ctx, 1, "хочу умереть")).notify_owner
    assert not run(process_text(ctx, 1, "хочу умереть")).notify_owner
    clock.t += 601
    assert run(process_text(ctx, 1, "хочу умереть")).notify_owner


def test_too_long():
    d = run(process_text(make_ctx(), 1, "а" * 201))
    assert d.kind is Kind.TOO_LONG


def test_rate_limit():
    ctx = make_ctx(limit=2)
    run(process_text(ctx, 1, "привет"))
    run(process_text(ctx, 1, "привет"))
    assert run(process_text(ctx, 1, "привет")).kind is Kind.RATE_LIMITED


def test_normal_answer_allows_sales():
    d = run(process_text(make_ctx(), 1, "сколько стоит?"))
    assert d.kind is Kind.ANSWER and d.allow_sales


# --- Виктория представляется -----------------------------------------------------
def test_victoria_introduces_herself_once():
    ctx = make_ctx(FaqAssistant(FaqBase.load()))
    d1 = run(process_text(ctx, 1, "сколько стоит?"))
    assert d1.reply.startswith(texts.VICTORIA_INTRO)
    assert "ИИ-помощница" in d1.reply and texts.VICTORIA_NAME_STORY in d1.reply
    d2 = run(process_text(ctx, 1, "что в наборе?"))
    assert texts.VICTORIA_INTRO not in d2.reply


def test_intro_constants():
    assert texts.VICTORIA_INTRO == "Я Виктория — ИИ-помощница проекта «Что сказать, когда…»."
    # никогда не выдаёт себя за человека
    assert "ИИ" in texts.victoria_intro()
    for t in (texts.WELCOME, texts.ASK_INTRO):
        assert "{intro}" in t


# --- вопросы без ответа → в сводку, без передачи владелице ---------------------
def test_unknown_question_goes_to_summary_without_pd():
    db = Storage(":memory:")
    ctx = make_ctx(FaqAssistant(FaqBase.load()), storage=db)
    d = run(process_text(ctx, 1, "какая погода на Марсе? пишите на a@b.ru или +7 999 123-45-67"))
    assert d.kind is Kind.ANSWER and not d.notify_owner
    assert "Этого в моей базе нет" in d.reply
    from datetime import timedelta
    from storage import utcnow
    qs = db.unanswered_between(utcnow() - timedelta(hours=1), utcnow() + timedelta(hours=1))
    assert len(qs) == 1 and "a@b.ru" not in qs[0] and "999" not in qs[0] and "Марсе" in qs[0]


# --- сценарий возврата ---------------------------------------------------------
@pytest.fixture
def db():
    s = Storage(":memory:")
    s.set_pd_consent(1, "Т")
    yield s
    s.close()


def test_refund_flow_swap_then_insist_then_email(db):
    db.add_purchase(1, "telefon")
    pid = db.last_purchase_id(1)
    db.set_marketing_consent(1, True)
    followup.on_purchase(db, 1, pid)             # «Как прошла неделя?» в очереди
    assert db.pending_for(1)
    spy = SpyAssistant()
    ctx = make_ctx(spy, storage=db, limit=50)
    # (а)+(б): признаёт и один раз предлагает замену кнопками
    d = run(process_text(ctx, 1, "Верните деньги, разбор ерунда"))
    assert d.kind is Kind.SERVICE and not d.notify_owner
    assert texts.REFUND_OFFER_SWAP in d.reply
    assert [b.callback for row in d.rows for b in row] == [f"fu:swap:{pid}:podrostok", f"fu:swap:{pid}:uroki"]
    # (в): настаивает → не спорит, просит e-mail
    d = run(process_text(ctx, 1, "нет, верните деньги"))
    assert d.reply == texts.REFUND_ASK_EMAIL
    d = run(process_text(ctx, 1, "не помню точно"))
    assert d.reply == texts.EMAIL_AGAIN
    d = run(process_text(ctx, 1, "Mama.Test@Mail.ru"))
    assert d.reply == texts.REFUND_ACCEPTED
    assert "до 10 рабочих дней" in d.reply and "зависит от банка" in d.reply
    reqs = db.refund_requests(open_only=True)
    assert len(reqs) == 1 and reqs[0]["email"] == "mama.test@mail.ru"
    assert reqs[0]["product"] == "telefon" and reqs[0]["purchase_id"] == pid
    assert db.is_no_sell(1)
    assert db.pending_for(1) == []                 # цепочка отменена
    assert spy.calls == []                         # ИИ в сценарии не участвовал
    # повторная просьба — заявка уже есть, без второй
    d = run(process_text(ctx, 1, "верните деньги"))
    assert d.reply.startswith(texts.REFUND_ALREADY) or texts.REFUND_ALREADY in d.reply
    assert len(db.refund_requests()) == 1
    # владелица отметила возврат
    assert db.mark_refunded(1) == 1 and db.refund_requests(open_only=True) == []


def test_refund_swap_offered_only_once(db):
    db.add_purchase(1, "telefon")
    ctx = make_ctx(storage=db)
    run(process_text(ctx, 1, "хочу вернуть деньги"))
    run(process_text(ctx, 1, "о чём разбор 2?"))      # сменила тему — сценарий закрыт
    d = run(process_text(ctx, 1, "всё-таки верните деньги"))
    assert d.reply == texts.REFUND_ASK_EMAIL         # замену второй раз не предлагает


def test_refund_with_email_in_first_message_after_offer(db):
    ctx = make_ctx(storage=db)                       # покупки в боте нет (файл пришёл на почту)
    d = run(process_text(ctx, 1, "верните деньги"))
    assert texts.REFUND_OFFER_SWAP_NO_RECORD in d.reply
    d = run(process_text(ctx, 1, "нет, только деньги, почта x@y.ru"))
    assert d.reply == texts.REFUND_ACCEPTED
    r = db.refund_requests()[0]
    assert r["product"] is None and r["email"] == "x@y.ru"


def test_refund_for_nabor_offers_gift(db):
    db.add_purchase(1, "nabor")
    pid = db.last_purchase_id(1)
    d = run(process_text(make_ctx(storage=db), 1, "верните деньги"))
    assert texts.REFUND_OFFER_GIFT in d.reply
    assert [b.callback for row in d.rows for b in row] == [f"fu:gift:{pid}"]


def test_wildberries_refund_by_marketplace_rules(db):
    d = run(process_text(make_ctx(storage=db), 1, "купила на вб, верните деньги"))
    assert texts.WB_REFUND in d.reply
    assert db.refund_requests() == [] and not db.is_no_sell(1)


# --- no_sell после заявки на возврат ---------------------------------------------
def test_no_sell_answers_have_no_sales(db):
    db.set_no_sell(1)
    ctx = make_ctx(FaqAssistant(FaqBase.load()), storage=db, limit=50)
    for q in ("сколько стоит разбор?", "хочу новый разбор про уроки, какой выбрать?", "как оплатить?",
              "есть скидка?", "что в наборе?"):
        d = run(process_text(ctx, 1, q))
        assert not d.allow_sales and d.rows == []
        assert "₽" not in d.reply and "оплатить" not in d.reply.lower(), (q, d.reply)
    # FAQ и кризис по-прежнему работают
    assert "PDF" in run(process_text(ctx, 1, "можно распечатать?")).reply
    assert run(process_text(ctx, 1, "хочу умереть")).reply == safety.CRISIS_REPLY


def test_no_sell_filters_llm_sales_text(db):
    db.set_no_sell(1)
    spy = SpyAssistant("Возьмите Разбор 3 за 299 ₽, оплатить можно в меню.")
    d = run(process_text(make_ctx(spy, storage=db), 1, "что посоветуете?"))
    assert spy.calls[-1][1] is True                  # ИИ получил запрет продавать
    assert "299" not in d.reply and texts.NO_SELL_ANSWER in d.reply


def test_no_sell_nofit_does_not_offer_swap(db):
    db.add_purchase(1, "telefon")
    db.set_no_sell(1)
    d = run(process_text(make_ctx(storage=db), 1, "разбор не подошёл"))
    assert texts.SWAP_AFTER_REFUND in d.reply and d.rows == []


def test_llm_refund_marker_starts_flow(db):
    spy = SpyAssistant("Поняла, помогу.", refund=True)
    d = run(process_text(make_ctx(spy, storage=db), 1, "хочу назад свои 299"))
    assert d.kind is Kind.SERVICE and texts.REFUND_OFFER_SWAP_NO_RECORD in d.reply


# --- файл, жалоба, мат, человек ----------------------------------------------------
def test_file_missing_resends_when_purchase_known(db):
    db.add_purchase(1, "uroki")
    d = run(process_text(make_ctx(storage=db), 1, "файл не пришёл"))
    assert d.resend_files and texts.FILE_RESEND in d.reply


def test_file_missing_without_purchase_collects_email(db):
    ctx = make_ctx(storage=db)
    d = run(process_text(ctx, 1, "оплатила, но файл не пришёл"))
    assert texts.FILE_HELP in d.reply and not d.resend_files
    d = run(process_text(ctx, 1, "q@w.ru"))
    assert d.reply == texts.FILE_REQUEST_ACCEPTED
    assert db.delivery_requests()[0]["email"] == "q@w.ru"


def test_complaint_profanity_human_no_escalation(db):
    ctx = make_ctx(storage=db)
    for text, expected in (("это обман", texts.COMPLAINT_REPLY),
                           ("да пошли вы на х*й", texts.PROFANITY_REPLY),
                           ("позовите живого человека", texts.HUMAN_REPLY)):
        d = run(process_text(ctx, 1, text))
        assert expected in d.reply and not d.notify_owner
    assert texts.PROFANITY_REPLY == "Готова помочь, если опишете, что не так."
