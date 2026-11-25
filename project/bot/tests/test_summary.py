"""Ежедневная сводка владелице: расписание (каждый день 10:00 МСК), формат, сроки по
заявлениям о возврате, отсутствие лишних ПДн. И no_sell в очереди отложенных сообщений. Без сети."""
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
def test_last_and_next_slot_daily_10_msk():
    assert summary.last_slot(at(2026, 11, 23, 10, 0)) == at(2026, 11, 23, 10, 0)
    assert summary.last_slot(at(2026, 11, 23, 9, 59)) == at(2026, 11, 22, 10, 0)
    assert summary.last_slot(at(2026, 11, 26, 15, 0)) == at(2026, 11, 26, 10, 0)
    assert summary.next_slot(at(2026, 11, 26, 15, 0)) == at(2026, 11, 27, 10, 0)
    assert summary.next_slot(at(2026, 11, 22, 9, 0)) == at(2026, 11, 22, 10, 0)
    # 07:30 UTC = 10:30 МСК
    from datetime import timezone
    assert summary.last_slot(datetime(2026, 11, 28, 7, 30, tzinfo=timezone.utc)) == at(2026, 11, 28, 10)


def test_due_slot_once_per_day_and_not_on_first_start(db):
    first = at(2026, 11, 25, 12)                      # первый запуск днём
    assert summary.due_slot(db, first) is None        # ничего не шлём, запоминаем слот
    assert summary.due_slot(db, at(2026, 11, 26, 9, 59)) is None
    slot = summary.due_slot(db, at(2026, 11, 26, 10, 1))
    assert slot == at(2026, 11, 26, 10, 0)
    summary.mark_sent(db, slot)
    assert summary.due_slot(db, at(2026, 11, 26, 18)) is None      # одно сообщение в день
    assert summary.due_slot(db, at(2026, 11, 27, 10, 0)) == at(2026, 11, 27, 10, 0)
    # бот был выключен два дня — после запуска одна сводка за последний слот
    summary.mark_sent(db, at(2026, 11, 27, 10, 0))
    assert summary.due_slot(db, at(2026, 11, 29, 15)) == at(2026, 11, 29, 10, 0)


# --- формат и персональные данные --------------------------------------------------
def test_summary_format_counts_refunds_questions(db):
    end = at(2026, 11, 30, 10)
    inside = end - timedelta(hours=5)
    old = end - timedelta(days=3)
    db.set_pd_consent(1, "Анна Секретная", inside)
    db.set_pd_consent(2, "Борис", old)
    db.add_purchase(1, "telefon", now=inside)
    db.add_purchase(1, "uroki", "swap", "swap-1", now=inside)
    rid = db.add_refund_request(1, "anna@mail.ru", db.get_purchase(1), now=inside,
                                reason="double", paid_on="29.11")
    db.add_delivery_request(2, "file", "boris@mail.ru", now=inside)
    db.add_unanswered(summary.scrub("а есть разбор про сон? пишите anna@mail.ru, +7 999 111-22-33"), inside)
    db.add_unanswered("старый вопрос", old)
    db.log_dialog(1, "user", "секретная переписка", inside)

    text = summary.build(db, end)
    assert text.startswith("Сводка за сутки 29.11 10:00–30.11.2026 10:00 (МСК)")
    assert "Новых пользователей: 1" in text
    assert "Покупок (записанных в боте): 1" in text
    assert "Замен разбора (и подарков вместо замены набора): 1" in text
    assert "Новых заявлений о возврате: 1" in text
    assert "Заявления о возврате без решения: 1" in text
    line = next(ln for ln in text.splitlines() if ln.startswith(f"№{rid} "))
    assert "осталось 10 дн. до 10-дневного срока" in line and "СРОЧНО" not in line
    assert "подано 30.11.2026" in line and "user_id 1" in line and "anna@mail.ru" in line
    assert "платёж прошёл дважды (дата платежей со слов клиента: 29.11)" in line
    assert "покупка 30.11.2026 · Телефон и игры" in line
    assert "/refunded <№>" in text and "/reject <№>" in text and "notify" in text
    assert "user_id 2 · e-mail оплаты boris@mail.ru · файл не пришёл" in text
    assert "Вопросы без ответа в базе: 1" in text and "сон" in text
    assert "старый вопрос" not in text
    # лишних ПДн нет: ни имён, ни переписки, ни контактов из вопросов
    for leaked in ("Анна", "Борис", "секретная переписка", "999", "+7"):
        assert leaked not in text, leaked
    assert text.count("anna@mail.ru") == 1          # только в строке заявления
    assert len(text) <= 4000


def test_days_left_and_urgent_highlight(db):
    end = at(2026, 11, 30, 10)
    # подано 20.11 → срок 30.11 (сегодня), 23.11 → 3 дня, 25.11 → 5 дней, 18.11 → прошёл 2 дня назад
    ids = {}
    for day in (25, 20, 23, 18):
        ids[day] = db.add_refund_request(day, f"u{day}@mail.ru", now=at(2026, 11, day, 23, 30),
                                         reason="other")
    db.add_refund_request(99, "x@mail.ru", now=at(2026, 11, 19, 12), reason="file")
    from support import resolve_refund
    resolve_refund(db, db.refund_requests()[-1]["id"], "rejected")        # решённое не показываем
    assert summary.days_left(at(2026, 11, 23, 23, 30), end) == 3
    # 23:30 МСК 20.11 = 20:30 UTC — дата подачи считается по МСК
    assert summary.days_left(at(2026, 11, 20, 23, 30), end) == 0
    text = summary.build(db, end)
    lines = [ln for ln in text.splitlines() if ln.startswith("№")]
    # сортировка: сначала самые срочные
    assert [ln.split(" ")[0] for ln in lines] == [f"№{ids[d]}" for d in (18, 20, 23, 25)]
    assert "СРОЧНО — СРОК ПРОШЁЛ 2 дн. назад" in lines[0]
    assert "СРОЧНО — срок истекает СЕГОДНЯ" in lines[1]
    assert "СРОЧНО — осталось 3 дн. до 10-дневного срока" in lines[2]
    assert "осталось 5 дн." in lines[3] and "СРОЧНО" not in lines[3]
    assert "СРОЧНО (осталось 3 дня или меньше): 3" in text
    assert "Заявления о возврате без решения: 4" in text and "x@mail.ru" not in text
    assert "другая причина" in lines[3]


def test_summary_hides_decided_refunds_and_crisis_only_as_count(db):
    from support import resolve_refund
    end = at(2026, 11, 30, 10)
    db.set_pd_consent(5, "Т", end - timedelta(hours=1))
    db.mark_crisis(5, end - timedelta(hours=1))
    rid = db.add_refund_request(5, "t@t.ru", None, now=end - timedelta(hours=1))
    resolve_refund(db, rid, "refunded")
    text = summary.build(db, end)
    assert "Заявления о возврате без решения: 0" in text
    assert "Срабатываний кризисного сценария: 1" in text
    assert "user_id 5" not in text


def test_scrub():
    t = summary.scrub("мой e-mail a.b@c.ru, тел 8 (999) 123-45-67, t.me/xxx, @nick " + "х" * 500)
    assert "a.b@c.ru" not in t and "123-45-67" not in t and "t.me" not in t and "@nick" not in t
    assert len(t) <= summary.MAX_QUESTION_LEN


def test_storage_migrates_old_refund_table(tmp_path):
    import sqlite3
    path = str(tmp_path / "old.sqlite3")
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE refund_requests (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER "
                "NOT NULL, email TEXT NOT NULL, purchase_id INTEGER, product TEXT, purchased_at TEXT, "
                "created_at TEXT NOT NULL, done_at TEXT)")
    con.execute("INSERT INTO refund_requests(user_id, email, created_at, done_at) "
                "VALUES (1, 'a@b.ru', '2026-11-01T00:00:00+00:00', '2026-11-02T00:00:00+00:00')")
    con.commit()
    con.close()
    s = Storage(path)
    r = s.refund_requests()[0]
    assert r["decision"] == "refunded" and r["reason"] is None
    s.add_refund_request(2, "c@d.ru", reason="file")
    assert s.refund_requests(open_only=True)[0]["reason"] == "file"
    s.close()


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
    assert len(sent) == 1 and sent[0].text.startswith("Сводка за сутки")
    assert asyncio.run(send_summary_if_due(bot, db, cfg, at(2026, 12, 1, 10, 0)))   # и на следующий день
    assert len(session.sent(1000)) == 2
