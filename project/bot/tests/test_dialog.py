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


def callbacks(rows):
    return [b.callback for row in rows for b in row]


REASON_CBS = ["rf:r:double", "rf:r:file", "rf:r:other"]


def test_refund_asks_reason_with_three_buttons(db):
    db.add_purchase(1, "telefon")
    spy = SpyAssistant()
    ctx = make_ctx(spy, storage=db)
    d = run(process_text(ctx, 1, "Верните деньги"))
    assert d.kind is Kind.SERVICE and not d.notify_owner
    assert texts.REFUND_ASK_REASON in d.reply
    assert callbacks(d.rows) == REASON_CBS
    assert [b.text for row in d.rows for b in row] == [
        "Платёж прошёл дважды", "Файл не пришёл или не открывается", "Другая причина"]
    assert spy.calls == [] and db.refund_requests() == []


# --- ветка «Платёж прошёл дважды» ------------------------------------------------
def test_refund_double_button_email_and_date(db):
    db.add_purchase(1, "telefon")
    ctx = make_ctx(storage=db, limit=50)
    run(process_text(ctx, 1, "верните деньги"))
    r = ctx.support.on_button(1, "rf:r:double")
    assert r.text == texts.REFUND_DOUBLE_ASK
    d = run(process_text(ctx, 1, "Mama.Test@Mail.ru"))          # без даты — переспросит дату
    assert d.reply == texts.REFUND_DOUBLE_ASK_DATE
    d = run(process_text(ctx, 1, "21.11"))
    assert d.reply == texts.REFUND_DOUBLE_ACCEPTED
    assert "в течение 10 дней" in d.reply
    req = db.refund_requests(open_only=True)
    assert len(req) == 1
    assert (req[0]["reason"], req[0]["email"], req[0]["paid_on"], req[0]["product"]) == \
        ("double", "mama.test@mail.ru", "21.11", "telefon")
    assert not db.is_no_sell(1)                      # no_sell — только после /refunded


def test_refund_double_detected_from_text_in_one_message(db):
    ctx = make_ctx(storage=db)
    d = run(process_text(ctx, 1, "Списали дважды, верните деньги"))
    assert d.reply.endswith(texts.REFUND_DOUBLE_ASK)
    d = run(process_text(ctx, 1, "pay@mail.ru, вчера"))
    assert d.reply == texts.REFUND_DOUBLE_ACCEPTED
    assert db.refund_requests()[0]["paid_on"] == "вчера"


def test_refund_double_without_date_after_one_retry_still_accepted(db):
    ctx = make_ctx(storage=db)
    ctx.support.on_button(1, "rf:r:double")
    run(process_text(ctx, 1, "pay@mail.ru"))
    d = run(process_text(ctx, 1, "не помню"))
    assert d.reply == texts.REFUND_DOUBLE_ACCEPTED
    assert db.refund_requests()[0]["paid_on"] is None


# --- ветка «Файл не пришёл или не открывается» ------------------------------------
def test_refund_file_resends_and_opened_closes_without_request(db):
    db.add_purchase(1, "uroki")
    ctx = make_ctx(storage=db)
    run(process_text(ctx, 1, "верните деньги"))
    r = ctx.support.on_button(1, "rf:r:file")
    assert r.resend_files and r.files_first
    assert r.text == texts.REFUND_FILE_RESEND and "Как открыть PDF" in r.text
    assert callbacks(r.rows) == ["rf:f:ok", "rf:f:bad"]
    assert [b.text for row in r.rows for b in row] == ["Открылось", "Всё равно не открывается"]
    r = ctx.support.on_button(1, "rf:f:ok")
    assert r.text == texts.REFUND_FILE_OK
    assert db.refund_requests() == [] and ctx.support.stage(1) is None


def test_refund_file_still_not_opening_then_email_then_request(db):
    db.add_purchase(1, "uroki")
    ctx = make_ctx(storage=db)
    d = run(process_text(ctx, 1, "файл не открывается, верните деньги"))   # причина видна сразу
    assert d.resend_files and d.files_first and callbacks(d.rows) == ["rf:f:ok", "rf:f:bad"]
    r = ctx.support.on_button(1, "rf:f:bad")
    assert r.text == texts.REFUND_FILE_ASK_EMAIL
    d = run(process_text(ctx, 1, "a@b.ru"))
    assert d.reply == texts.REFUND_ACCEPTED == "Принимаю ваше заявление, его рассмотрят в течение 10 дней."
    req = db.refund_requests()[0]
    assert req["reason"] == "file" and req["email"] == "a@b.ru" and req["product"] == "uroki"
    assert not db.is_no_sell(1)


def test_refund_file_text_answer_not_opening(db):
    db.add_purchase(1, "uroki")
    ctx = make_ctx(storage=db)
    ctx.introduced.add(1)                            # представилась раньше
    ctx.support.on_button(1, "rf:r:file")
    d = run(process_text(ctx, 1, "всё равно не открывается"))
    assert d.reply == texts.REFUND_FILE_ASK_EMAIL


def test_refund_file_without_purchase_first_delivers_file(db):
    ctx = make_ctx(storage=db)
    ctx.introduced.add(1)                            # представилась раньше
    ctx.support.on_button(1, "rf:r:file")
    d = run(process_text(ctx, 1, "q@w.ru"))
    assert d.reply.startswith(texts.FILE_REQUEST_ACCEPTED) and texts.REFUND_FILE_REQUEST_NOTE in d.reply
    assert db.delivery_requests()[0]["email"] == "q@w.ru"
    assert db.refund_requests() == []               # сначала выдача файла, не возврат


# --- ветка «Другая причина» ---------------------------------------------------------
def test_refund_other_offers_swap_then_insist_then_email(db):
    db.add_purchase(1, "telefon")
    pid = db.last_purchase_id(1)
    db.set_marketing_consent(1, True)
    followup.on_purchase(db, 1, pid)             # «Как прошла неделя?» в очереди
    spy = SpyAssistant()
    ctx = make_ctx(spy, storage=db, limit=50)
    run(process_text(ctx, 1, "Верните деньги, разбор ерунда"))
    r = ctx.support.on_button(1, "rf:r:other")
    assert r.text == texts.REFUND_OTHER_SWAP and "14 дней" in r.text
    assert callbacks(r.rows) == [f"fu:swap:{pid}:podrostok", f"fu:swap:{pid}:uroki"]
    d = run(process_text(ctx, 1, "нет, верните деньги"))
    assert d.reply == texts.REFUND_OTHER_ASK_EMAIL
    d = run(process_text(ctx, 1, "не помню точно"))
    assert d.reply == texts.EMAIL_AGAIN
    d = run(process_text(ctx, 1, "Mama.Test@Mail.ru"))
    assert d.reply == "Принимаю ваше заявление, его рассмотрят в течение 10 дней."
    for banned in ("верн", "закон", "ЗоЗПП", "статья", "отказ"):
        assert banned not in d.reply
    req = db.refund_requests(open_only=True)
    assert len(req) == 1 and req[0]["reason"] == "other" and req[0]["purchase_id"] == pid
    # no_sell НЕ ставится при подаче заявления, продающие сообщения не отменяются
    assert not db.is_no_sell(1) and db.pending_for(1)
    assert spy.calls == []                         # ИИ в сценарии не участвовал
    # повторная просьба — заявление уже есть, второго нет
    d = run(process_text(ctx, 1, "верните деньги"))
    assert texts.REFUND_ALREADY in d.reply
    assert ctx.support.on_button(1, "rf:r:other").text == texts.REFUND_ALREADY
    assert len(db.refund_requests()) == 1


def test_refund_other_reason_typed_as_text(db):
    db.add_purchase(1, "telefon")
    ctx = make_ctx(storage=db)
    run(process_text(ctx, 1, "хочу вернуть деньги"))
    d = run(process_text(ctx, 1, "просто не понравилось"))
    assert d.reply == texts.REFUND_OTHER_SWAP and d.rows


def test_refund_other_swap_offered_only_once(db):
    db.add_purchase(1, "telefon")
    ctx = make_ctx(storage=db)
    ctx.support.on_button(1, "rf:r:other")
    run(process_text(ctx, 1, "о чём разбор 2?"))      # сменила тему — сценарий закрыт
    r = ctx.support.on_button(1, "rf:r:other")
    assert r.text == texts.REFUND_OTHER_ASK_EMAIL     # замену второй раз не предлагает


def test_refund_other_without_record_email_in_same_message(db):
    ctx = make_ctx(storage=db)                       # покупки в боте нет (файл пришёл на почту)
    ctx.introduced.add(1)
    r = ctx.support.on_button(1, "rf:r:other")
    assert r.text == texts.REFUND_OTHER_SWAP_NO_RECORD
    d = run(process_text(ctx, 1, "нет, только деньги, почта x@y.ru"))
    assert d.reply == texts.REFUND_ACCEPTED
    req = db.refund_requests()[0]
    assert req["product"] is None and req["email"] == "x@y.ru" and req["reason"] == "other"


def test_refund_other_for_nabor_offers_gift(db):
    db.add_purchase(1, "nabor")
    pid = db.last_purchase_id(1)
    ctx = make_ctx(storage=db)
    ctx.introduced.add(1)                            # представилась раньше
    r = ctx.support.on_button(1, "rf:r:other")
    assert r.text == texts.REFUND_OTHER_GIFT
    assert callbacks(r.rows) == [f"fu:gift:{pid}"]
    d = run(process_text(ctx, 1, "давайте подарок"))
    assert d.reply == texts.REFUND_PICK_SWAP and callbacks(d.rows) == [f"fu:gift:{pid}"]


def test_refund_other_after_14_days_goes_straight_to_application(db):
    from datetime import timedelta
    from storage import utcnow
    db.add_purchase(1, "telefon", now=utcnow() - timedelta(days=15))
    ctx = make_ctx(storage=db)
    r = ctx.support.on_button(1, "rf:r:other")
    assert r.text == texts.REFUND_OTHER_ASK_EMAIL
    pid = db.last_purchase_id(1)
    assert followup.check_swap(db, 1, pid, "uroki").reply == texts.SWAP_EXPIRED


def test_refund_texts_do_not_promise_or_argue():
    for name in ("REFUND_ASK_REASON", "REFUND_OTHER_SWAP", "REFUND_OTHER_GIFT",
                 "REFUND_OTHER_SWAP_NO_RECORD", "REFUND_OTHER_ASK_EMAIL", "REFUND_ACCEPTED",
                 "REFUND_ALREADY", "REFUND_FILE_ASK_EMAIL", "REFUND_FILE_RESEND"):
        t = getattr(texts, name).lower()
        for banned in ("оформлю возврат", "вернём", "верну деньги", "закон", "зозпп", "статья",
                       "не положен", "доступ"):
            assert banned not in t, (name, banned)


def test_resolve_refund_no_sell_only_after_refunded(db):
    import support
    db.set_marketing_consent(1, True)
    db.add_purchase(1, "telefon")
    followup.on_purchase(db, 1, db.last_purchase_id(1))
    a = db.add_refund_request(1, "a@b.ru", reason="other")
    assert not db.is_no_sell(1)
    # отказ: заявление закрыто, no_sell нет, сообщения не отменены
    req = support.resolve_refund(db, a, "rejected")
    assert req["decision"] == "rejected" and req["done_at"]
    assert not db.is_no_sell(1) and db.pending_for(1)
    assert support.resolve_refund(db, a, "refunded") is None        # уже решено
    # возврат выполнен: no_sell + отмена продающих сообщений
    b = db.add_refund_request(1, "a@b.ru", reason="file")
    req = support.resolve_refund(db, b, "refunded")
    assert req["decision"] == "refunded" and db.is_no_sell(1) and db.pending_for(1) == []
    assert support.resolve_refund(db, 999, "refunded") is None


def test_wildberries_refund_by_marketplace_rules(db):
    d = run(process_text(make_ctx(storage=db), 1, "купила на вб, верните деньги"))
    assert texts.WB_REFUND in d.reply
    assert db.refund_requests() == [] and not db.is_no_sell(1)


# --- no_sell после выполненного возврата ---------------------------------------------
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
    assert d.kind is Kind.SERVICE and texts.REFUND_ASK_REASON in d.reply
    assert callbacks(d.rows) == REASON_CBS


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
