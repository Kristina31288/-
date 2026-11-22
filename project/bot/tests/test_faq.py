"""Режим без ИИ: ответы из faq.json по ключевым словам."""
import asyncio
import json
from pathlib import Path

import pytest

from faq import DEFAULT_FAQ_PATH, FaqBase
from llm import FaqAssistant

BOT_DIR = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def faq():
    return FaqBase.load()


def test_faq_has_at_least_15_items(faq):
    assert len(faq.items) >= 15
    ids = [i.id for i in faq.items]
    assert len(ids) == len(set(ids))


@pytest.mark.parametrize("question,expected_id", [
    ("Сколько стоит разбор?", "price"),
    ("какая цена", "price"),
    ("Что входит в набор?", "bundle"),
    ("Что внутри разбора?", "what_is"),
    ("Как оплатить?", "pay"),
    ("Когда придёт файл после оплаты?", "delivery"),
    ("А если не подойдёт?", "refund_rule"),
    ("Вы психолог?", "who"),
    ("Это гарантирует результат?", "result"),
    ("Есть скидка?", "discount"),
    ("Промокод есть?", "discount"),
    ("Хочу записаться на консультацию", "consult"),
    ("Ты бот?", "is_ai"),
    ("Можно распечатать?", "format"),
    ("Какие данные вы храните?", "privacy"),
    ("Здравствуйте", "greeting"),
    ("Какой разбор выбрать, сыну 9", "which"),
])
def test_faq_match(faq, question, expected_id):
    m = faq.match(question)
    assert m.found, question
    assert m.item.id == expected_id, (question, m.item.id)


def test_faq_unknown_returns_fallback(faq):
    text, found = faq.answer("какая погода на Марсе")
    assert not found
    assert "передам" in text


def test_faq_prices_match_brand(faq):
    by_id = {i.id: i for i in faq.items}
    price = by_id["price"].answer
    for p in ("299 ₽", "690 ₽", "1 500 ₽"):
        assert p in price
    assert "7 дней" in by_id["refund_rule"].answer


def test_faq_answers_do_not_promise_or_mislabel(faq):
    for item in faq.items:
        low = item.answer.lower()
        assert "гарантируем" not in low
        assert "детский психолог" not in low or "не детский психолог" in low
        assert "осталось" not in low


def test_faq_assistant_handoff_flag():
    a = FaqAssistant(FaqBase.load())
    r = asyncio.run(a.answer("сколько стоит"))
    assert not r.handoff and "299" in r.text
    r = asyncio.run(a.answer("ываыва"))
    assert r.handoff


def test_faq_json_is_valid_and_has_fallback():
    data = json.loads(DEFAULT_FAQ_PATH.read_text(encoding="utf-8"))
    assert data["fallback"]
    for item in data["items"]:
        assert item["keywords"] and item["answer"]


def test_knowledge_has_products_and_15_questions():
    kb = (BOT_DIR / "knowledge.md").read_text(encoding="utf-8")
    for s in ("299 ₽", "690 ₽", "1 500 ₽", "8-800-2000-122", "7 дней"):
        assert s in kb
    assert "15." in kb
