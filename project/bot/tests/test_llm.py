"""ИИ-слой без сети: промпт, разбор ответа, выбор реализации, запасной режим."""
import asyncio

from config import load_config
from faq import FaqBase
import texts
from llm import (REFUND_MARKER, UNKNOWN_MARKER, FaqAssistant, GigaChatAssistant,
                 build_system_prompt, load_knowledge, looks_like_sales, make_assistant,
                 parse_model_output)

BASE = {"BOT_TOKEN": "1:A", "OWNER_ID": "1"}


def test_system_prompt_contains_rules_and_knowledge():
    p = build_system_prompt(load_knowledge())
    for s in ("ИИ", "Не обещай результат", "платёжные данные", "8-800-2000-122", "112",
              UNKNOWN_MARKER, REFUND_MARKER, "299 ₽", "690 ₽", "7 дней", "Этого в моей базе нет",
              texts.VICTORIA_INTRO, "Никогда не выдавай себя за человека", "Не раскрывай"):
        assert s in p, s
    for s in ("без нытья", "2–5 предложений", "сначала конкретика"):   # правило тона
        assert s in p.lower(), s
    # методика продаж и возражения
    for s in ("ОДИН разбор", "набор", "Дорого", "Есть бесплатно", "Не поможет", "Подумаю",
              "Вы психолог?", "не назвав цену"):
        assert s in p, s
    # решения владелицы: нет консультаций и передач автору
    low = p.lower()
    assert "консультац" not in low.replace("личных консультаций нет", "")
    assert "1 500" not in p
    assert "передам вопрос автору" not in low
    assert "музык" not in p.lower()          # в промпте нет следов чужих шаблонов
    assert "СЕЙЧАС НЕ ПРОДАВАЙ" not in p
    assert "СЕЙЧАС НЕ ПРОДАВАЙ" in build_system_prompt("kb", no_sales=True)


def test_parse_model_output():
    r = parse_model_output(f"Этого в моей базе нет. {UNKNOWN_MARKER}")
    assert r.unknown and UNKNOWN_MARKER not in r.text
    r = parse_model_output(f"Поняла, помогу. {REFUND_MARKER}")
    assert r.refund and REFUND_MARKER not in r.text
    r = parse_model_output("Разбор 1 стоит 299 ₽.")
    assert not r.unknown and not r.refund
    assert parse_model_output("").unknown
    assert len(parse_model_output("а" * 10000).text) <= 3500


def test_sales_filter():
    assert looks_like_sales("Разбор стоит 299 ₽, оплатить можно в меню")
    assert looks_like_sales("Купите набор")
    assert not looks_like_sales("Файл придёт на e-mail, указанный при оплате.")
    assert not looks_like_sales(texts.NO_SELL_ANSWER)


def test_make_assistant_without_key_is_faq():
    assert isinstance(make_assistant(load_config(dict(BASE))), FaqAssistant)


def test_make_assistant_with_key_is_gigachat():
    a = make_assistant(load_config({**BASE, "GIGACHAT_AUTH_KEY": "x"}), knowledge="kb")
    assert isinstance(a, GigaChatAssistant)


def test_gigachat_falls_back_to_faq_on_error():
    a = GigaChatAssistant("key", "kb", FaqAssistant(FaqBase.load()))

    async def boom(messages):
        raise ConnectionError("нет сети")

    a._complete = boom
    r = asyncio.run(a.answer("сколько стоит"))
    assert r.source == "faq" and "299" in r.text


def test_gigachat_messages_shape():
    a = GigaChatAssistant("key", "kb", FaqAssistant(FaqBase.load()))
    msgs = a.build_messages("вопрос", [("user", "a"), ("bot", "b")], no_sales=False)
    assert [m["role"] for m in msgs] == ["system", "user", "assistant", "user"]
    assert msgs[-1]["content"] == "вопрос"
