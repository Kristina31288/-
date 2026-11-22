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
    ("А если не подойдёт?", "swap"),
    ("Вы психолог?", "who"),
    ("Какие у вас есть разборы?", "catalog"),
    ("Как открыть pdf?", "open_pdf"),
    ("Чем открыть файл", "open_pdf"),
    ("Сыну 13, какой разбор?", "which"),
    ("Дорого", "expensive"),
    ("Я подумаю", "think"),
    ("Зачем платить, если в интернете всё есть", "free_internet"),
    ("Вы человек?", "is_ai"),
    ("Купила на Wildberries", "wb"),
    ("Правда, что фразы проверены подростком?", "teen_checked"),
    ("В каком колледже учится дочь автора?", "daughter_private"),
    ("Из какого города дочка автора?", "daughter_private"),
    ("Это гарантирует результат?", "result"),
    ("Есть скидка?", "discount"),
    ("Промокод есть?", "discount"),
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
    assert text.startswith("Этого в моей базе нет")
    assert "автор" not in text.lower()


def test_faq_prices_match_brand(faq):
    by_id = {i.id: i for i in faq.items}
    price = by_id["price"].answer
    for p in ("299 ₽", "690 ₽"):
        assert p in price
    assert "1 500" not in price
    assert "подберём другой" in by_id["swap"].answer


def test_faq_answers_do_not_promise_or_mislabel(faq):
    for item in faq.items:
        low = item.answer.lower()
        assert "гарантируем" not in low
        assert "детский психолог" not in low or "не детский психолог" in low
        assert "осталось" not in low


def test_faq_assistant_unknown_flag_and_no_sell_answers():
    a = FaqAssistant(FaqBase.load())
    r = asyncio.run(a.answer("сколько стоит"))
    assert not r.unknown and "299" in r.text
    r = asyncio.run(a.answer("ываыва"))
    assert r.unknown
    # без продаж (после кризиса / no_sell): ни цен, ни оплаты
    for q in ("сколько стоит", "какой разбор выбрать", "как оплатить", "есть скидка", "дорого",
              "что в наборе", "какие есть разборы"):
        r = asyncio.run(a.answer(q, no_sales=True))
        assert "₽" not in r.text and "оплатить" not in r.text.lower(), (q, r.text)


def test_faq_has_no_consultation_or_author_handoff():
    raw = DEFAULT_FAQ_PATH.read_text(encoding="utf-8").lower()
    for banned in ("консультац", "передам вопрос автору", "позовите автора", "написать автору",
                   "живой человек", "вернём деньги", "7 дней после покупки"):
        assert banned not in raw, banned


def test_daughter_private_data_never_disclosed(faq):
    kb = (BOT_DIR / "knowledge.md").read_text(encoding="utf-8")
    assert "студентка педагогического колледжа" in kb and "Это личная информация" in kb
    a = {i.id: i for i in faq.items}["daughter_private"].answer
    assert a.startswith("Это личная информация")


def test_faq_who_never_calls_author_just_psychologist(faq):
    who = {i.id: i for i in faq.items}["who"].answer.lower()
    assert "психолог по образованию" in who
    assert "не детский психолог" in who


def test_faq_json_is_valid_and_has_fallback():
    data = json.loads(DEFAULT_FAQ_PATH.read_text(encoding="utf-8"))
    assert data["fallback"]
    for item in data["items"]:
        assert item["keywords"] and item["answer"]


def test_knowledge_has_products_and_15_questions():
    kb = (BOT_DIR / "knowledge.md").read_text(encoding="utf-8")
    for s in ("299 ₽", "690 ₽", "8-800-2000-122", "7 дней", "Виктория", "Карта спокойного вечера",
              "до 10 рабочих дней", "Wildberries", "Проверено подростком", "Кристина Плетнева"):
        assert s in kb
    assert "1 500" not in kb and "Консультация автора" not in kb
    assert "15." in kb
