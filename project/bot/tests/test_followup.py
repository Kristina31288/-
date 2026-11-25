"""Автоцепочка и «Как прошла неделя?»: расписание, тихие часы, отмены, отсутствие повторной продажи.

Только стандартная библиотека: без aiogram, сети и Telegram.
"""
from datetime import datetime, timedelta, timezone

import pytest

import followup as fu
import texts
from config import load_config
from storage import Storage

MSK = fu.MSK
UID = 7          # смещение в окне: 7 минут → 19:07


def at(y, mo, d, h, mi=0):
    """Время по Москве → aware datetime."""
    return datetime(y, mo, d, h, mi, tzinfo=MSK)


@pytest.fixture
def db(tmp_path):
    s = Storage(str(tmp_path / "bot.sqlite3"))
    yield s
    s.close()


@pytest.fixture
def cfg(tmp_path):
    return load_config({
        "BOT_TOKEN": "1:x", "OWNER_ID": "1",
        "PAY_LINK_TELEFON": "https://pay.example/1", "PAY_LINK_PODROSTOK": "https://pay.example/2",
        "PAY_LINK_UROKI": "https://pay.example/3", "PAY_LINK_NABOR": "https://pay.example/all",
        "FILES_DIR": str(tmp_path),
    })


def subscribed(db, uid=UID, now=None):
    db.set_pd_consent(uid, "Тест", now)
    db.set_marketing_consent(uid, True, now)


def run_due(db, now):
    """Имитирует один проход планировщика без Telegram: возвращает отправленные виды."""
    sent = []
    for m in db.due_messages(now):
        v = fu.check(db, m, now)
        if v.action == "cancel":
            db.cancel_message(m.id, v.reason)
        elif v.action == "reschedule":
            db.reschedule(m.id, v.due)
        else:
            fu.build_scheduled(db, cfg_stub(), m)
            fu.after_sent(db, m, now)
            sent.append(m.kind)
    return sent


def cfg_stub():
    return load_config({"BOT_TOKEN": "1:x", "OWNER_ID": "1"})


# --- расписание и тихие часы ----------------------------------------------------
def test_chain_plan_days_and_window():
    start = at(2026, 11, 22, 12, 0)
    plan = dict(fu.plan_chain(UID, start))
    assert plan[fu.TIP1] == at(2026, 11, 23, 19, 7)
    assert plan[fu.TIP2] == at(2026, 11, 24, 19, 7)
    assert plan[fu.TIP3] == at(2026, 11, 25, 19, 7)
    for due in plan.values():
        assert fu.in_chain_window(due)


def test_chain_offset_stays_inside_window():
    for uid in (0, 59, 119, 120, 10**9 + 7):
        slot = fu.chain_slot(at(2026, 11, 23, 0).date(), uid)
        assert fu.in_chain_window(slot)


def test_chain_plan_late_night_consent_is_next_evening():
    plan = dict(fu.plan_chain(UID, at(2026, 11, 22, 23, 50)))
    assert plan[fu.TIP1] == at(2026, 11, 23, 19, 7)


def test_next_chain_time():
    assert fu.next_chain_time(at(2026, 11, 22, 20, 0), UID) == at(2026, 11, 22, 20, 0)
    assert fu.next_chain_time(at(2026, 11, 22, 10, 0), UID) == at(2026, 11, 22, 19, 7)
    assert fu.next_chain_time(at(2026, 11, 22, 21, 0), UID) == at(2026, 11, 23, 19, 7)
    assert fu.next_chain_time(at(2026, 11, 22, 23, 30), UID) == at(2026, 11, 23, 19, 7)


def test_quiet_hours_and_week_plan():
    assert fu.is_quiet(at(2026, 11, 22, 22, 0))
    assert fu.is_quiet(at(2026, 11, 22, 3, 0))
    assert fu.is_quiet(at(2026, 11, 22, 8, 59))
    assert not fu.is_quiet(at(2026, 11, 22, 9, 0))
    assert not fu.is_quiet(at(2026, 11, 22, 21, 59))
    # выдали днём — через 3 дня в то же время
    assert fu.plan_week(at(2026, 11, 22, 15, 0)) == at(2026, 11, 25, 15, 0)
    # выдали в 23:00 — через 3 дня попадает в тихие часы → 10:00 следующего дня
    assert fu.plan_week(at(2026, 11, 22, 23, 0)) == at(2026, 11, 26, 10, 0)
    # выдали в 02:00 — 10:00 того же дня
    assert fu.plan_week(at(2026, 11, 22, 2, 0)) == at(2026, 11, 25, 10, 0)


def test_utc_input_is_converted_to_moscow():
    utc = datetime(2026, 11, 22, 16, 30, tzinfo=timezone.utc)     # 19:30 МСК
    assert fu.in_chain_window(utc)
    assert fu.is_quiet(datetime(2026, 11, 22, 19, 0, tzinfo=timezone.utc))   # 22:00 МСК


def test_full_chain_is_sent_once_and_in_window(db):
    start = at(2026, 11, 22, 12, 0)
    subscribed(db, now=start)
    assert fu.start_chain(db, UID, start) == 3
    assert fu.start_chain(db, UID, start) == 0                    # повторное согласие — без дублей
    assert run_due(db, at(2026, 11, 23, 12, 0)) == []              # рано
    assert run_due(db, at(2026, 11, 23, 19, 10)) == [fu.TIP1]
    assert run_due(db, at(2026, 11, 24, 19, 10)) == [fu.TIP2]
    assert run_due(db, at(2026, 11, 25, 19, 10)) == [fu.TIP3]
    # мягкий повтор — через 2 дня, ровно один
    assert run_due(db, at(2026, 11, 27, 19, 10)) == [fu.TIP3_REPEAT]
    assert run_due(db, at(2026, 12, 10, 19, 10)) == []
    kinds = [m.kind for m in db.scheduled_for(UID)]
    assert kinds.count(fu.TIP3_REPEAT) == 1


def test_missed_window_is_rescheduled_not_sent_at_night(db):
    start = at(2026, 11, 22, 12, 0)
    subscribed(db, now=start)
    fu.start_chain(db, UID, start)
    # бот лежал вечером, поднялся в 23:30 — не шлём, переносим на завтра 19:07
    assert run_due(db, at(2026, 11, 23, 23, 30)) == []
    tip1 = [m for m in db.pending_for(UID) if m.kind == fu.TIP1][0]
    assert tip1.due_at == at(2026, 11, 24, 19, 7)


def test_week_rescheduled_from_quiet_hours(db):
    db.set_pd_consent(UID, "Т")
    db.add_purchase(UID, "telefon")
    db.schedule_message(UID, fu.WEEK, at(2026, 11, 25, 15, 0), once=False, ref=1)
    assert run_due(db, at(2026, 11, 25, 23, 0)) == []
    week = db.pending_for(UID)[0]
    assert week.due_at == at(2026, 11, 26, 10, 0)
    assert run_due(db, at(2026, 11, 26, 10, 1)) == [fu.WEEK]


# --- согласие -------------------------------------------------------------------
def test_chain_requires_marketing_consent(db):
    start = at(2026, 11, 22, 12, 0)
    db.set_pd_consent(UID, "Т", start)          # согласие на ПДн есть, на рассылку — нет
    fu.start_chain(db, UID, start)
    assert run_due(db, at(2026, 11, 23, 19, 10)) == []
    assert db.scheduled_for(UID)[0].cancelled


# --- отмены: кризис, отписка, удаление, блокировка ---------------------------------
def test_crisis_cancels_sales_for_7_days(db):
    start = at(2026, 11, 22, 12, 0)
    subscribed(db, now=start)
    fu.start_chain(db, UID, start)
    n = fu.on_crisis(db, UID, at(2026, 11, 22, 18, 0))
    assert n == 3 and db.pending_for(UID) == []
    assert fu.sales_paused(db, UID, at(2026, 11, 28, 18, 0))
    assert not fu.sales_paused(db, UID, at(2026, 11, 29, 18, 1))


def test_crisis_blocks_message_due_after_cancel_window(db):
    """Сообщение, поставленное позже кризиса, не уйдёт, пока не прошло 7 дней."""
    db.set_pd_consent(UID, "Т")
    db.add_purchase(UID, "telefon")
    fu.on_crisis(db, UID, at(2026, 11, 22, 12, 0))
    db.schedule_message(UID, fu.WEEK, at(2026, 11, 25, 12, 0), once=False, ref=1)
    assert run_due(db, at(2026, 11, 25, 12, 0)) == []
    assert db.scheduled_for(UID)[-1].cancelled


def test_crisis_for_unknown_user_stores_nothing(db):
    assert fu.on_crisis(db, 999) == 0
    assert db.followup_state(999).crisis_at is None


def test_stop_tips_cancels_and_is_stored(db):
    start = at(2026, 11, 22, 12, 0)
    subscribed(db, now=start)
    fu.start_chain(db, UID, start)
    assert fu.stop_tips(db, UID, start) == 3
    assert db.followup_state(UID).tips_off
    # даже если что-то поставили позже — не уйдёт
    db.schedule_message(UID, fu.TIP3_REPEAT, at(2026, 11, 23, 19, 7))
    assert run_due(db, at(2026, 11, 23, 19, 10)) == []


def test_delete_me_removes_queue_and_state(db):
    start = at(2026, 11, 22, 12, 0)
    subscribed(db, now=start)
    fu.start_chain(db, UID, start)
    fu.stop_tips(db, UID)
    assert db.delete_user(UID)
    assert db.scheduled_for(UID) == []
    assert not db.followup_state(UID).tips_off


def test_message_for_deleted_user_is_cancelled(db):
    db.schedule_message(UID, fu.TIP1, at(2026, 11, 23, 19, 7))   # пользователя нет
    assert run_due(db, at(2026, 11, 23, 19, 10)) == []
    assert db.scheduled_for(UID)[0].cancelled


def test_blocked_user_gets_nothing(db):
    start = at(2026, 11, 22, 12, 0)
    subscribed(db, now=start)
    fu.start_chain(db, UID, start)
    fu.on_blocked(db, UID, start)
    assert db.pending_for(UID) == []
    db.schedule_message(UID, fu.WEEK, at(2026, 11, 23, 12, 0), once=False)
    assert fu.check(db, db.pending_for(UID)[0], at(2026, 11, 23, 12, 0)).reason == "blocked"
    db.clear_blocked(UID)                       # вернулся через /start
    assert not db.followup_state(UID).blocked


# --- без повторной продажи ------------------------------------------------------
def test_purchase_stops_chain_and_schedules_week(db):
    start = at(2026, 11, 22, 12, 0)
    subscribed(db, now=start)
    fu.start_chain(db, UID, start)
    run_due(db, at(2026, 11, 23, 19, 10))
    db.add_purchase(UID, "telefon")
    fu.on_purchase(db, UID, db.last_purchase_id(UID), at(2026, 11, 23, 20, 0))
    assert [m.kind for m in db.pending_for(UID)] == [fu.WEEK]
    assert db.pending_for(UID)[0].due_at == at(2026, 11, 26, 20, 0)
    assert run_due(db, at(2026, 11, 30, 19, 10)) == [fu.WEEK]


def test_repeat_not_sent_after_purchase(db):
    start = at(2026, 11, 22, 12, 0)
    subscribed(db, now=start)
    fu.start_chain(db, UID, start)
    for d in (23, 24, 25):
        run_due(db, at(2026, 11, d, 19, 10))
    db.add_purchase(UID, "uroki")               # купил по ссылке, не через /give
    assert run_due(db, at(2026, 11, 27, 19, 10)) == []


def test_repeat_scheduled_only_once(db):
    start = at(2026, 11, 22, 12, 0)
    subscribed(db, now=start)
    db.schedule_message(UID, fu.TIP3, at(2026, 11, 25, 19, 7))
    m = db.pending_for(UID)[0]
    fu.after_sent(db, m, at(2026, 11, 25, 19, 10))
    fu.after_sent(db, m, at(2026, 11, 25, 19, 11))
    assert [x.kind for x in db.scheduled_for(UID)].count(fu.TIP3_REPEAT) == 1


def test_repeat_uses_chosen_situation(db, cfg):
    subscribed(db)
    db.set_chosen_product(UID, "podrostok")
    db.schedule_message(UID, fu.TIP3_REPEAT, at(2026, 11, 27, 19, 7))
    out = fu.build_scheduled(db, cfg, db.pending_for(UID)[0])
    assert "299" in out[0].text and "Больше напоминать не буду" in out[0].text
    assert out[0].rows[0][0].url == "https://pay.example/2"


# --- карточка по кнопке ----------------------------------------------------------
@pytest.mark.parametrize("key,link", [("telefon", "/1"), ("podrostok", "/2"), ("uroki", "/3")])
def test_card_by_button(cfg, key, link):
    card, nabor = fu.build_card(cfg, key, owned=set())
    assert fu.catalog.PRODUCTS[key].title in card.text
    assert "299\u00a0₽" in card.text                       # цена в тексте — до кнопки оплаты
    assert card.rows[0][0].url.endswith(link)
    assert "690\u00a0₽" in nabor.text and nabor.rows[0][0].url.endswith("/all")


def test_tip3_has_three_situations_and_stop():
    out = fu.build_scheduled(None, cfg_stub(), fu.Scheduled(1, UID, fu.TIP3, at(2026, 1, 1, 19), None,
                                                            False, None))
    cbs = [b.callback for row in out[0].rows for b in row]
    assert cbs == ["fu:sit:telefon", "fu:sit:podrostok", "fu:sit:uroki", "fu:stop"]
    assert [b.text for row in out[0].rows[:3] for b in row] == [
        "Телефон и игры", "Грубит или молчит", "Уроки и учёба"]


def test_card_without_pay_link(tmp_path):
    card, _ = fu.build_card(cfg_stub(), "telefon", owned=set())
    assert card.rows == [] and texts.PAY_LINK_MISSING in card.text


def test_card_for_owned_guide_does_not_sell_it(cfg):
    out = fu.build_card(cfg, "telefon", owned={"telefon"})
    assert out[0].rows == [] and "уже есть" in out[0].text
    assert fu.build_card(cfg, "telefon", owned={"telefon", "podrostok", "uroki"})[1:] == []


# --- «Как прошла неделя?» ---------------------------------------------------------
def test_week_calm_one_guide_offers_upgrade_placeholder(db, cfg):
    db.set_pd_consent(UID, "Т")
    db.add_purchase(UID, "telefon")
    out = fu.build_week_answer(db, cfg, UID, "calm", db.last_purchase_id(UID))
    assert "набор со скидкой на уже купленный: [ССЫЛКА_ДОПЛАТА]" in out[0].text
    urls = [b.url for row in out[0].rows for b in row]
    assert urls == ["https://pay.example/2", "https://pay.example/3"]


def test_week_calm_uses_upgrade_link(db, tmp_path):
    cfg = load_config({"BOT_TOKEN": "1:x", "OWNER_ID": "1",
                       "PAY_LINK_UPGRADE": "https://pay.example/up"})
    db.set_pd_consent(UID, "Т")
    db.add_purchase(UID, "telefon")
    out = fu.build_week_answer(db, cfg, UID, "calm", db.last_purchase_id(UID))
    assert "https://pay.example/up" in out[0].text


def test_week_hard_resends_lead_magnet_without_consultation(db, cfg):
    db.set_pd_consent(UID, "Т")
    db.add_purchase(UID, "telefon")
    out = fu.build_week_answer(db, cfg, UID, "hard", db.last_purchase_id(UID))
    assert out[0].send_lead_magnet
    assert "8-800-2000-122" in out[-1].text
    joined = " ".join(o.text for o in out).lower()
    assert "консультац" not in joined and "1 500" not in joined


def test_week_nofit_offers_swap_and_one_swap_per_purchase(db, cfg):
    db.set_pd_consent(UID, "Т")
    db.add_purchase(UID, "telefon")
    pid = db.last_purchase_id(UID)
    out = fu.build_week_answer(db, cfg, UID, "nofit", pid)
    assert out[0].text == texts.WEEK_NOFIT_SWAP
    assert [b.callback for row in out[0].rows for b in row] == [
        f"fu:swap:{pid}:podrostok", f"fu:swap:{pid}:uroki"]
    assert fu.check_swap(db, UID, pid, "telefon").ok is False      # уже есть
    assert fu.check_swap(db, UID, pid, "uroki").ok
    assert fu.record_swap(db, UID, pid, "uroki")
    assert not fu.record_swap(db, UID, pid, "podrostok")           # вторая замена — нет
    assert fu.check_swap(db, UID, pid, "podrostok").reply == texts.SWAP_ALREADY
    assert [p.source for p in db.list_purchases(UID)] == ["manual", "swap"]
    assert fu.build_week_answer(db, cfg, UID, "nofit", pid)[0].text == texts.SWAP_ALREADY


def test_swap_rejects_foreign_purchase_and_nabor(db, cfg):
    db.set_pd_consent(UID, "Т")
    db.set_pd_consent(8, "Ч")
    db.add_purchase(8, "telefon")
    foreign = db.last_purchase_id(8)
    assert not fu.check_swap(db, UID, foreign, "uroki").ok
    db.add_purchase(UID, "nabor")
    nabor = db.last_purchase_id(UID)
    assert not fu.check_swap(db, UID, nabor, "uroki").ok
    assert fu.build_week_answer(db, cfg, UID, "nofit", nabor)[0].text == texts.WEEK_NOFIT_NABOR


def test_chain_and_week_texts_follow_owner_rules():
    """Владелица не общается с клиентами: в новых текстах нет возврата, автора, консультации."""
    names = ["TIP1", "TIP2", "TIP3", "CARD", "NABOR_OFFER", "TIP3_REPEAT", "TIP3_REPEAT_CHOSEN",
             "WEEK", "WEEK_CALM", "WEEK_CALM_UPGRADE", "WEEK_CALM_OTHER", "WEEK_CALM_ALL",
             "WEEK_HARD", "WEEK_HARD_MORE", "WEEK_HARD_UPGRADE", "WEEK_HARD_SAFETY",
             "WEEK_NOFIT_SWAP", "WEEK_NOFIT_NABOR", "WEEK_NOFIT_NOTHING", "SWAP_DONE",
             "SWAP_ALREADY", "SWAP_FILE_LATER", "SWAP_PRODUCT_OWNED", "TIPS_STOPPED"]
    for n in names:
        t = getattr(texts, n).lower()
        for banned in ("возврат", "верните", "вернём", "автор", "живой человек", "консультац"):
            assert banned not in t, (n, banned)
    assert texts.TIP1.startswith("Попробовали первую фразу?")
    assert texts.TIP3.startswith("Какая ситуация у вас сейчас?")
    assert texts.WEEK.startswith("Как прошла неделя")
