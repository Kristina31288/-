"""Конвейер обработки текста: кризис → длина → антиспам → передача → ответ."""
import asyncio

import safety
from dialog import DialogContext, Kind, process_text
from faq import FaqBase
from llm import AssistantReply, FaqAssistant


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


class SpyAssistant:
    """Запоминает вызовы; по умолчанию «продаёт»."""

    def __init__(self, reply="Разбор 1 стоит 299 ₽", handoff=False):
        self.calls = []
        self.reply = AssistantReply(reply, handoff=handoff)

    async def answer(self, text, history=(), no_sales=False):
        self.calls.append((text, no_sales))
        return self.reply


def make_ctx(assistant=None, limit=5, clock=None):
    clock = clock or FakeClock()
    return DialogContext(
        assistant=assistant or SpyAssistant(),
        max_len=200,
        limiter=safety.RateLimiter(limit, 60, clock=clock),
        crisis_cooldown=safety.Cooldown(3600, clock=clock),
        owner_notify_cooldown=safety.Cooldown(600, clock=clock),
    )


def run(coro):
    return asyncio.run(coro)


def test_crisis_does_not_reach_assistant_and_does_not_sell():
    spy = SpyAssistant()
    ctx = make_ctx(spy)
    d = run(process_text(ctx, 1, "Дочь сказала, что не хочет жить"))
    assert d.kind is Kind.CRISIS
    assert d.reply == safety.CRISIS_REPLY
    assert d.notify_owner and not d.allow_sales and not d.log_user_text
    assert spy.calls == []


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
    ctx = make_ctx(clock=clock)
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


def test_refund_goes_to_owner_not_to_ai():
    spy = SpyAssistant()
    d = run(process_text(make_ctx(spy), 1, "Верните деньги"))
    assert d.kind is Kind.HANDOFF and d.notify_owner
    assert spy.calls == []


def test_normal_answer_allows_sales():
    d = run(process_text(make_ctx(), 1, "сколько стоит?"))
    assert d.kind is Kind.ANSWER and d.allow_sales


def test_unknown_question_in_faq_mode_is_handed_off():
    ctx = make_ctx(FaqAssistant(FaqBase.load()))
    d = run(process_text(ctx, 1, "какая погода на Марсе"))
    assert d.kind is Kind.HANDOFF and d.notify_owner
