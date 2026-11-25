import asyncio
from datetime import timedelta

import pytest

from payments import GrantError, PaymentEvent, grant
from storage import Storage, utcnow


@pytest.fixture
def db(tmp_path):
    s = Storage(str(tmp_path / "sub" / "bot.sqlite3"))
    yield s
    s.close()


def test_consent_flow(db):
    assert not db.has_consent(1)
    db.upsert_user(1, "Анна")
    assert not db.has_consent(1)
    db.set_pd_consent(1, "Анна")
    u = db.get_user(1)
    assert u.has_consent and u.first_name == "Анна" and not u.marketing_consent
    first = u.pd_consent_at
    db.set_pd_consent(1, "Анна")                 # повтор не меняет дату согласия
    assert db.get_user(1).pd_consent_at == first


def test_marketing_consent(db):
    db.set_pd_consent(1, "А")
    db.set_pd_consent(2, "Б")
    db.set_marketing_consent(1, True)
    assert db.marketing_recipients() == [1]
    db.set_marketing_consent(1, False)
    assert db.marketing_recipients() == []


def test_purchases_and_idempotency(db):
    db.set_pd_consent(1, "А")
    assert db.add_purchase(1, "telefon")
    assert db.add_purchase(1, "telefon")         # ручные выдачи без external_id не схлопываются
    assert db.add_purchase(1, "nabor", "yookassa", "pay-1")
    assert not db.add_purchase(1, "nabor", "yookassa", "pay-1")
    assert [p.product for p in db.list_purchases(1)] == ["telefon", "telefon", "nabor"]


def test_delete_me_removes_everything(db):
    db.set_pd_consent(1, "А")
    db.add_purchase(1, "uroki")
    db.log_dialog(1, "user", "вопрос")
    db.set_pd_consent(2, "Б")
    assert db.delete_user(1)
    assert db.get_user(1) is None
    assert db.list_purchases(1) == []
    assert db.dialog(1) == []
    assert db.get_user(2) is not None          # чужие данные не тронуты
    assert not db.delete_user(1)


def test_purge_old_dialogs(db):
    now = utcnow()
    db.log_dialog(1, "user", "старое", now=now - timedelta(days=40))
    db.log_dialog(1, "user", "новое", now=now)
    assert db.purge_old_dialogs(30, now=now) == 1
    assert db.dialog(1) == [("user", "новое")]


def test_no_child_fields_in_schema(db):
    cols = {r[1] for r in db._conn.execute("PRAGMA table_info(users)")}
    assert cols == {"user_id", "first_name", "pd_consent_at", "marketing_consent",
                    "marketing_consent_at", "created_at"}


def test_memory_db():
    s = Storage(":memory:")
    s.set_pd_consent(5, None)
    assert s.has_consent(5)
    s.close()


def test_grant_from_payment_event(db):
    sent = []

    async def send(uid, product):
        sent.append((uid, product.key))

    db.set_pd_consent(7, "В")
    ev = PaymentEvent("yookassa", "p-1", 7, "nabor", 690)
    assert asyncio.run(grant(ev, db, send)) is True
    assert asyncio.run(grant(ev, db, send)) is False     # повтор вебхука — без второй выдачи
    assert sent == [(7, "nabor")]
    with pytest.raises(GrantError):
        asyncio.run(grant(PaymentEvent("yookassa", "p-2", 7, "nabor", 10), db, send))
    with pytest.raises(GrantError):
        asyncio.run(grant(PaymentEvent("yookassa", "p-3", 999, "telefon", 299), db, send))
