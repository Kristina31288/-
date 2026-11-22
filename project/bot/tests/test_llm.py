"""ИИ-слой без сети: промпт, разбор ответа, выбор реализации, запасной режим."""
import asyncio

from config import load_config
from faq import FaqBase
from llm import (HANDOFF_MARKER, FaqAssistant, GigaChatAssistant, build_system_prompt,
                 load_knowledge, make_assistant, parse_model_output)

BASE = {"BOT_TOKEN": "1:A", "OWNER_ID": "1"}


def test_system_prompt_contains_rules_and_knowledge():
    p = build_system_prompt(load_knowledge())
    for s in ("ИИ", "Не обещай результат", "платёжные данные", "8-800-2000-122", "112",
              HANDOFF_MARKER, "299 ₽", "690 ₽", "1 500 ₽", "7 дней"):
        assert s in p, s
    assert "музык" not in p.lower()          # шаблон из agents.md адаптирован под проект
    assert "СЕЙЧАС НЕ ПРОДАВАЙ" not in p
    assert "СЕЙЧАС НЕ ПРОДАВАЙ" in build_system_prompt("kb", no_sales=True)


def test_parse_model_output():
    r = parse_model_output(f"Этого я не знаю, передам вопрос автору. {HANDOFF_MARKER}")
    assert r.handoff and HANDOFF_MARKER not in r.text
    r = parse_model_output("Разбор 1 стоит 299 ₽.")
    assert not r.handoff
    assert parse_model_output("").handoff
    assert len(parse_model_output("а" * 10000).text) <= 3500


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
