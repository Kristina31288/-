"""Еженедельная сводка владелице: расписание (пн 10:00 МСК), формат, отсутствие лишних ПДн.
И no_sell в очереди отложенных сообщений. Без сети."""
import asyncio
from datetime import datetime, timedelta

import pytest

import followup as fu
import summary
from followup import MSK
from storage import Storage


def at(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=MSK)


@pytest.fixture
def db(tmp_path):
    s = Storage(str(tmp_path / "bot.sqlite3"))
    yield s
    s.close()


# --- расписание ------------------------------------------------------------------
def test_last_and_next_slot_monday_10_msk():
    # 2026-11-23 — понедельник
    assert summary.last_slot(at(2026, 11, 23, 10, 0)) == at(2026, 11, 23, 10, 0)
    assert summary.last_slot(at(2026, 11, 23, 9, 59)) == at(2026, 11, 16, 10, 0)
    assert summary.last_slot(at(2026, 11, 26, 15, 0)) == at(2026, 11, 23, 10, 0)
    assert summary.next_slot(at(2026, 11, 26, 15, 0)) == at(2026, 11, 30, 10, 0)
    assert summary.next_slot(at(2026, 11, 22, 23, 0)).weekday() == 0


def test_due_slot_once_per_week_and_not_on_first_start(db):
    first = at(2026, 11, 25, 12)                      # первый запуск в среду
    assert summary.due_slot(db, first) is None        # ничего не шлём, запоминаем слот
    assert summary.due_slot(db, at(2026, 11, 30, 9, 59)) is None
    slot = summary.due_slot(db, at(2026, 11, 30, 10, 1))
    assert slot == at(2026, 11, 30, 10, 0)
    summary.mark_sent(db, slot)
    assert summary.due_slot(db, at(2026, 11, 30, 18)) is None      # одно сообщение в неделю
    assert summary.due_slot(db, at(2026, 12, 7, 10, 0)) == at(2026, 12, 7, 10, 0)


# --- формат и персональные данные --------------------------------------------------
def test_summary_format_counts_refunds_questions(db):
    end = at(2026, 11, 30, 10)
    inside = end - timedelta(days=2)
    old = end - timedelta(days=10)
    db.set_pd_consent(1, "Анна Секретная", inside)
    db.set_pd_consent(2, "Борис", old)
    db.add_purchase(1, "telefon", now=inside)
    db.add_purchase(1, "uroki", "swap", "swap-1", now=inside)
    db.add_refund_request(1, "anna@mail.ru", db.get_purchase(1), now=inside)
    db.add_delivery_request(2, "file", "boris@mail.ru", now=inside)
    db.add_unanswered(summary.scrub("а есть разбор про сон? пишите anna@mail.ru, +7 999 111-22-33"), inside)
    db.add_unanswered("старый вопрос", old)
    db.log_dialog(1, "user", "секретная переписка", inside)

    text = summary.build(db, end)
    assert text.startswith("Сводка за неделю 23.11.2026–30.11.2026")
    assert "Новых пользователей: 1" in text
    assert "Покупок (записанных в боте): 1" in text
    assert "Замен разбора (и подарков вместо замены набора): 1" in text
    assert "Заявок на возврат за неделю: 1" in text
    assert "Возвраты к оформлению: 1" in text
    assert "1. user_id 1 · e-mail оплаты anna@mail.ru · покупка 28.11.2026 · Телефон и игры" in text
    assert "/refunded <user_id>" in text
    assert "user_id 2 · e-mail оплаты boris@mail.ru · файл не пришёл" in text
    assert "Вопросы без ответа в базе: 1" in text and "сон" in text
    assert "старый вопрос" not in text
    # лишних ПДн нет: ни имён, ни переписки, ни контактов из вопросов
    for leaked in ("Анна", "Борис", "секретная переписка", "999", "+7"):
        assert leaked not in text, leaked
    assert text.count("anna@mail.ru") == 1          # только в строке заявки на возврат
    assert len(text) <= 4000


def test_summary_marks_done_refunds_and_crisis_only_as_count(db):
    end = at(2026, 11, 30, 10)
    db.set_pd_consent(5, "Т", end - timedelta(days=1))
    db.mark_crisis(5, end - timedelta(days=1))
    db.add_refund_request(5, "t@t.ru", None, now=end - timedelta(days=1))
    db.mark_refunded(5)
    text = summary.build(db, end)
    assert "Возвраты к оформлению: 0" in text
    assert "Срабатываний кризисного сценария: 1" in text
    assert "user_id 5" not in text


def test_scrub():
    t = summary.scrub("мой e-mail a.b@c.ru, тел 8 (999) 123-45-67, t.me/xxx, @nick " + "х" * 500)
    assert "a.b@c.ru" not in t and "123-45-67" not in t and "t.me" not in t and "@nick" not in t
    assert len(t) <= summary.MAX_QUESTION_LEN


def test_delete_me_keeps_only_open_refund_and_no_sell(db):
    db.set_pd_consent(9, "Т")
    db.add_refund_request(9, "x@y.ru")
    db.set_no_sell(9)
    db.add_delivery_request(9, "file", "x@y.ru")
    assert db.delete_user(9)
    assert db.get_user(9) is None
    assert len(db.refund_requests(open_only=True, user_id=9)) == 1
    assert db.is_no_sell(9) and db.delivery_requests() == []


# --- no_sell в очереди ---------------------------------------------------------------
def test_no_sell_cancels_chain_and_week(db):
    now = at(2026, 11, 22, 12)
    db.set_pd_consent(7, "Т", now)
    db.set_marketing_consent(7, True, now)
    fu.start_chain(db, 7, now)
    db.set_no_sell(7)
    for m in db.pending_for(7):
        assert fu.check(db, m, m.due_at).reason == "no_sell"
    assert 7 not in db.marketing_recipients()


# --- отправка сводки планировщиком ------------------------------------------------
def test_scheduler_sends_summary_to_owner_once(db):
    pytest.importorskip("aiogram")
    from aiogram import Bot

    from config import load_config
    from scheduler import send_summary_if_due
    from test_handlers_smoke import TOKEN, FakeSession

    cfg = load_config({"BOT_TOKEN": TOKEN, "OWNER_ID": "1000"})
    session = FakeSession()
    bot = Bot(TOKEN, session=session)
    assert not asyncio.run(send_summary_if_due(bot, db, cfg, at(2026, 11, 25, 12)))
    assert asyncio.run(send_summary_if_due(bot, db, cfg, at(2026, 11, 30, 10, 0)))
    assert not asyncio.run(send_summary_if_due(bot, db, cfg, at(2026, 11, 30, 10, 1)))
    sent = session.sent(1000)
    assert len(sent) == 1 and sent[0].text.startswith("Сводка за неделю")
