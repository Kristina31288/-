"""Хранилище SQLite (стандартная библиотека).

Что храним (минимум по 152-ФЗ):
- users: Telegram user_id, имя из профиля Telegram, дата согласия на обработку ПДн,
  согласие на рассылку (и его дата).
- purchases: что и когда выдано пользователю.
- scheduled_messages: очередь сообщений автоцепочки и «Как прошла неделя?»
  (вид, когда отправить, отправлено/отменено). Без текстов.
- followup_state: отписка от советов, отметка «заблокировал бота», время последнего
  срабатывания кризисного детектора (без текста), выбранная ситуация.
- dialog_log: вопросы к ИИ-помощнице и ответы, хранятся DIALOG_LOG_DAYS дней
  (для разбора спорных случаев). Тексты кризисных сообщений НЕ сохраняются —
  только отметка, что сработал кризисный сценарий.
- refund_requests: заявления о возврате (user_id, e-mail оплаты, покупка, основание
  double | file | other, дата платежа со слов клиента для double). Владелица видит их
  в ежедневной сводке со сроком «осталось N дней из 10» и закрывает командой
  /refunded <№> (возврат выполнен) или /reject <№> (отказ).
- delivery_requests: «файл не пришёл» / замена без записи о покупке в боте
  (user_id, e-mail оплаты) — владелица выдаёт файл командой /give.
- no_sell: отметка «не продавать» (только user_id и дата) — ставится навсегда, когда
  владелица отметила возврат выполненным (/refunded). Подача заявления и отказ (/reject)
  её не ставят.
- unanswered_questions: вопросы, на которые не нашлось ответа в базе, БЕЗ user_id;
  e-mail, телефоны и ссылки вырезаются до записи (summary.scrub).
- meta: служебные значения (когда отправлена последняя ежедневная сводка).

Данные детей не запрашиваются и не хранятся (полей для них нет).
"""
from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id               INTEGER PRIMARY KEY,
    first_name            TEXT,
    pd_consent_at         TEXT,
    marketing_consent     INTEGER NOT NULL DEFAULT 0,
    marketing_consent_at  TEXT,
    created_at            TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS purchases (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    product     TEXT NOT NULL,
    source      TEXT NOT NULL,          -- manual | prodamus | yookassa | ...
    external_id TEXT,                   -- id платежа в платёжном сервисе (для идемпотентности)
    granted_at  TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS purchases_external
    ON purchases(source, external_id) WHERE external_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS dialog_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    role       TEXT NOT NULL,           -- user | bot | event
    text       TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS dialog_log_user ON dialog_log(user_id);
CREATE INDEX IF NOT EXISTS dialog_log_created ON dialog_log(created_at);
CREATE TABLE IF NOT EXISTS scheduled_messages (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL,
    kind          TEXT NOT NULL,        -- tip1 | tip2 | tip3 | tip3_repeat | week
    due_at        TEXT NOT NULL,
    sent_at       TEXT,
    cancelled     INTEGER NOT NULL DEFAULT 0,
    cancel_reason TEXT,
    ref           INTEGER,              -- для week: id покупки
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS scheduled_due ON scheduled_messages(sent_at, cancelled, due_at);
CREATE INDEX IF NOT EXISTS scheduled_user ON scheduled_messages(user_id);
CREATE TABLE IF NOT EXISTS refund_requests (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL,
    email        TEXT NOT NULL,         -- e-mail, на который была оплата (со слов клиента)
    purchase_id  INTEGER,               -- последняя покупка в боте, если есть
    product      TEXT,                  -- ключ продукта или NULL (покупки в боте нет)
    purchased_at TEXT,
    reason       TEXT,                  -- double | file | other (основание со слов клиента)
    paid_on      TEXT,                  -- для double: дата платежа со слов клиента
    created_at   TEXT NOT NULL,          -- дата подачи заявления: от неё считается 10-дневный срок
    done_at      TEXT,                  -- /refunded <№> или /reject <№>
    decision     TEXT                   -- refunded | rejected
);
CREATE INDEX IF NOT EXISTS refund_user ON refund_requests(user_id);
CREATE TABLE IF NOT EXISTS delivery_requests (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    kind       TEXT NOT NULL,           -- file | swap
    email      TEXT NOT NULL,
    product    TEXT,                    -- для замены: какой разбор хочет (если понятно)
    created_at TEXT NOT NULL,
    done_at    TEXT                     -- /give этому пользователю
);
CREATE TABLE IF NOT EXISTS no_sell (
    user_id INTEGER PRIMARY KEY,
    since   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS unanswered_questions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    question   TEXT NOT NULL,           -- без user_id, e-mail/телефоны/ссылки вырезаны
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS followup_state (
    user_id        INTEGER PRIMARY KEY,
    tips_off_at    TEXT,                -- /stop_tips или кнопка «Не присылать советы»
    blocked_at     TEXT,                -- пользователь заблокировал бота
    crisis_at      TEXT,                -- последняя отметка кризисного детектора (без текста)
    chosen_product TEXT                 -- ситуация, выбранная в сообщении 3 цепочки
);
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class User:
    user_id: int
    first_name: str | None
    pd_consent_at: str | None
    marketing_consent: bool
    marketing_consent_at: str | None
    created_at: str

    @property
    def has_consent(self) -> bool:
        return self.pd_consent_at is not None


@dataclass(frozen=True)
class Scheduled:
    id: int
    user_id: int
    kind: str
    due_at: datetime
    sent_at: str | None
    cancelled: bool
    ref: int | None


@dataclass(frozen=True)
class FollowupState:
    tips_off: bool = False
    blocked: bool = False
    crisis_at: datetime | None = None
    chosen_product: str | None = None


def _parse(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


@dataclass(frozen=True)
class Purchase:
    id: int
    user_id: int
    product: str
    source: str
    external_id: str | None
    granted_at: str


class Storage:
    def __init__(self, path: str) -> None:
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        if path != ":memory:":
            self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.executescript(SCHEMA)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        """Добавляет колонки, которых нет в базе от прошлых версий бота."""
        have = {r["name"] for r in self._conn.execute("PRAGMA table_info(refund_requests)")}
        for col in ("reason", "paid_on", "decision"):
            if col not in have:
                self._conn.execute(f"ALTER TABLE refund_requests ADD COLUMN {col} TEXT")
        # старые выполненные заявки (до появления /reject) — это выполненные возвраты
        self._conn.execute("UPDATE refund_requests SET decision = 'refunded' "
                           "WHERE done_at IS NOT NULL AND decision IS NULL")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # --- пользователи -----------------------------------------------------
    def upsert_user(self, user_id: int, first_name: str | None, now: datetime | None = None) -> None:
        now = now or utcnow()
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO users(user_id, first_name, created_at) VALUES (?, ?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET first_name = excluded.first_name",
                (user_id, (first_name or "")[:64] or None, _iso(now)),
            )

    def get_user(self, user_id: int) -> User | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
        if row is None:
            return None
        return User(
            user_id=row["user_id"],
            first_name=row["first_name"],
            pd_consent_at=row["pd_consent_at"],
            marketing_consent=bool(row["marketing_consent"]),
            marketing_consent_at=row["marketing_consent_at"],
            created_at=row["created_at"],
        )

    def has_consent(self, user_id: int) -> bool:
        u = self.get_user(user_id)
        return bool(u and u.has_consent)

    def set_pd_consent(self, user_id: int, first_name: str | None, now: datetime | None = None) -> None:
        now = now or utcnow()
        self.upsert_user(user_id, first_name, now)
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE users SET pd_consent_at = COALESCE(pd_consent_at, ?) WHERE user_id = ?",
                (_iso(now), user_id),
            )

    def set_marketing_consent(self, user_id: int, value: bool, now: datetime | None = None) -> None:
        now = now or utcnow()
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE users SET marketing_consent = ?, marketing_consent_at = ? WHERE user_id = ?",
                (1 if value else 0, _iso(now), user_id),
            )

    def marketing_recipients(self) -> list[int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT user_id FROM users WHERE marketing_consent = 1 AND pd_consent_at IS NOT NULL "
                "AND user_id NOT IN (SELECT user_id FROM no_sell)"
            ).fetchall()
        return [r["user_id"] for r in rows]

    def count_users(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]

    # --- покупки ----------------------------------------------------------
    def add_purchase(self, user_id: int, product: str, source: str = "manual",
                     external_id: str | None = None, now: datetime | None = None) -> bool:
        """Записывает выдачу. False — если такой платёж уже был учтён (повтор вебхука)."""
        now = now or utcnow()
        try:
            with self._lock, self._conn:
                self._conn.execute(
                    "INSERT INTO purchases(user_id, product, source, external_id, granted_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (user_id, product, source, external_id, _iso(now)),
                )
        except sqlite3.IntegrityError:
            return False
        return True

    def get_purchase(self, purchase_id: int) -> Purchase | None:
        with self._lock:
            r = self._conn.execute("SELECT * FROM purchases WHERE id = ?", (purchase_id,)).fetchone()
        if r is None:
            return None
        return Purchase(r["id"], r["user_id"], r["product"], r["source"], r["external_id"],
                        r["granted_at"])

    def last_purchase_id(self, user_id: int) -> int | None:
        with self._lock:
            r = self._conn.execute("SELECT MAX(id) FROM purchases WHERE user_id = ?",
                                   (user_id,)).fetchone()
        return r[0]

    def list_purchases(self, user_id: int) -> list[Purchase]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM purchases WHERE user_id = ? ORDER BY id", (user_id,)
            ).fetchall()
        return [Purchase(r["id"], r["user_id"], r["product"], r["source"], r["external_id"],
                         r["granted_at"]) for r in rows]

    # --- журнал диалогов --------------------------------------------------
    def log_dialog(self, user_id: int, role: str, text: str, now: datetime | None = None) -> None:
        now = now or utcnow()
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO dialog_log(user_id, role, text, created_at) VALUES (?, ?, ?, ?)",
                (user_id, role, text[:4000], _iso(now)),
            )

    def dialog(self, user_id: int) -> list[tuple[str, str]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT role, text FROM dialog_log WHERE user_id = ? ORDER BY id", (user_id,)
            ).fetchall()
        return [(r["role"], r["text"]) for r in rows]

    def purge_old_dialogs(self, days: int, now: datetime | None = None) -> int:
        now = now or utcnow()
        border = _iso(now - timedelta(days=days))
        with self._lock, self._conn:
            cur = self._conn.execute("DELETE FROM dialog_log WHERE created_at < ?", (border,))
        return cur.rowcount

    # --- очередь отложенных сообщений ------------------------------------
    def schedule_message(self, user_id: int, kind: str, due_at: datetime, *, once: bool = True,
                         ref: int | None = None, now: datetime | None = None) -> bool:
        """Ставит сообщение в очередь. once=True — не ставит, если такое уже было когда-либо
        (отправлено, отменено или ждёт); once=False — не ставит, только если такое уже ждёт."""
        now = now or utcnow()
        cond = "" if once else " AND sent_at IS NULL AND cancelled = 0"
        with self._lock, self._conn:
            exists = self._conn.execute(
                f"SELECT 1 FROM scheduled_messages WHERE user_id = ? AND kind = ?{cond}",
                (user_id, kind)).fetchone()
            if exists:
                return False
            self._conn.execute(
                "INSERT INTO scheduled_messages(user_id, kind, due_at, ref, created_at) "
                "VALUES (?, ?, ?, ?, ?)", (user_id, kind, _iso(due_at), ref, _iso(now)))
        return True

    @staticmethod
    def _scheduled(r: sqlite3.Row) -> Scheduled:
        return Scheduled(r["id"], r["user_id"], r["kind"], datetime.fromisoformat(r["due_at"]),
                         r["sent_at"], bool(r["cancelled"]), r["ref"])

    def due_messages(self, now: datetime | None = None, limit: int = 100) -> list[Scheduled]:
        now = now or utcnow()
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM scheduled_messages WHERE sent_at IS NULL AND cancelled = 0 "
                "AND due_at <= ? ORDER BY due_at, id LIMIT ?", (_iso(now), limit)).fetchall()
        return [self._scheduled(r) for r in rows]

    def scheduled_for(self, user_id: int) -> list[Scheduled]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM scheduled_messages WHERE user_id = ? ORDER BY id", (user_id,)
            ).fetchall()
        return [self._scheduled(r) for r in rows]

    def pending_for(self, user_id: int) -> list[Scheduled]:
        return [m for m in self.scheduled_for(user_id) if m.sent_at is None and not m.cancelled]

    def mark_sent(self, message_id: int, now: datetime | None = None) -> None:
        now = now or utcnow()
        with self._lock, self._conn:
            self._conn.execute("UPDATE scheduled_messages SET sent_at = ? WHERE id = ?",
                               (_iso(now), message_id))

    def cancel_message(self, message_id: int, reason: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE scheduled_messages SET cancelled = 1, cancel_reason = ? WHERE id = ?",
                (reason, message_id))

    def reschedule(self, message_id: int, due_at: datetime) -> None:
        with self._lock, self._conn:
            self._conn.execute("UPDATE scheduled_messages SET due_at = ? WHERE id = ?",
                               (_iso(due_at), message_id))

    def cancel_pending(self, user_id: int, kinds: tuple[str, ...] | None = None,
                       reason: str = "", before: datetime | None = None) -> int:
        """Отменяет ждущие сообщения пользователя (все или только kinds; до before — если задано)."""
        sql = ("UPDATE scheduled_messages SET cancelled = 1, cancel_reason = ? "
               "WHERE user_id = ? AND sent_at IS NULL AND cancelled = 0")
        args: list = [reason, user_id]
        if kinds:
            sql += f" AND kind IN ({','.join('?' * len(kinds))})"
            args += list(kinds)
        if before is not None:
            sql += " AND due_at < ?"
            args.append(_iso(before))
        with self._lock, self._conn:
            return self._conn.execute(sql, args).rowcount

    # --- состояние цепочки (отписка, блокировка, кризис) ------------------
    def followup_state(self, user_id: int) -> FollowupState:
        with self._lock:
            r = self._conn.execute("SELECT * FROM followup_state WHERE user_id = ?",
                                   (user_id,)).fetchone()
        if r is None:
            return FollowupState()
        return FollowupState(tips_off=r["tips_off_at"] is not None,
                             blocked=r["blocked_at"] is not None,
                             crisis_at=_parse(r["crisis_at"]),
                             chosen_product=r["chosen_product"])

    def _set_state(self, user_id: int, column: str, value: str | None) -> None:
        assert column in {"tips_off_at", "blocked_at", "crisis_at", "chosen_product"}
        with self._lock, self._conn:
            self._conn.execute(
                f"INSERT INTO followup_state(user_id, {column}) VALUES (?, ?) "
                f"ON CONFLICT(user_id) DO UPDATE SET {column} = excluded.{column}",
                (user_id, value))

    def set_tips_off(self, user_id: int, now: datetime | None = None) -> None:
        self._set_state(user_id, "tips_off_at", _iso(now or utcnow()))

    def mark_blocked(self, user_id: int, now: datetime | None = None) -> None:
        self._set_state(user_id, "blocked_at", _iso(now or utcnow()))

    def clear_blocked(self, user_id: int) -> None:
        with self._lock, self._conn:
            self._conn.execute("UPDATE followup_state SET blocked_at = NULL WHERE user_id = ?",
                               (user_id,))

    def mark_crisis(self, user_id: int, now: datetime | None = None) -> None:
        self._set_state(user_id, "crisis_at", _iso(now or utcnow()))

    def set_chosen_product(self, user_id: int, product: str) -> None:
        self._set_state(user_id, "chosen_product", product)

    # --- no_sell: после выполненного возврата (/refunded) не продаём никогда --
    def set_no_sell(self, user_id: int, now: datetime | None = None) -> None:
        with self._lock, self._conn:
            self._conn.execute("INSERT OR IGNORE INTO no_sell(user_id, since) VALUES (?, ?)",
                               (user_id, _iso(now or utcnow())))

    def is_no_sell(self, user_id: int) -> bool:
        with self._lock:
            return self._conn.execute("SELECT 1 FROM no_sell WHERE user_id = ?",
                                      (user_id,)).fetchone() is not None

    # --- заявления о возврате ---------------------------------------------
    REFUND_REASONS = ("double", "file", "other")

    def add_refund_request(self, user_id: int, email: str, purchase: "Purchase | None" = None,
                           now: datetime | None = None, *, reason: str = "other",
                           paid_on: str | None = None) -> int:
        assert reason in self.REFUND_REASONS
        now = now or utcnow()
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO refund_requests(user_id, email, purchase_id, product, purchased_at, "
                "reason, paid_on, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (user_id, email[:254], purchase.id if purchase else None,
                 purchase.product if purchase else None,
                 purchase.granted_at if purchase else None, reason,
                 (paid_on or None) and paid_on[:40], _iso(now)))
        return cur.lastrowid

    def refund_requests(self, *, open_only: bool = False, user_id: int | None = None) -> list[dict]:
        sql, args = "SELECT * FROM refund_requests WHERE 1 = 1", []
        if open_only:
            sql += " AND done_at IS NULL"
        if user_id is not None:
            sql += " AND user_id = ?"
            args.append(user_id)
        with self._lock:
            rows = self._conn.execute(sql + " ORDER BY id", args).fetchall()
        return [dict(r) for r in rows]

    def get_refund_request(self, request_id: int) -> dict | None:
        with self._lock:
            r = self._conn.execute("SELECT * FROM refund_requests WHERE id = ?",
                                   (request_id,)).fetchone()
        return dict(r) if r else None

    def close_refund_request(self, request_id: int, decision: str,
                             now: datetime | None = None) -> dict | None:
        """Закрывает открытое заявление решением refunded | rejected.

        Возвращает заявление (уже закрытое) или None, если такого открытого заявления нет.
        Отметку no_sell здесь НЕ ставим: это делает обработчик /refunded (support.on_refunded).
        """
        assert decision in ("refunded", "rejected")
        with self._lock, self._conn:
            n = self._conn.execute(
                "UPDATE refund_requests SET done_at = ?, decision = ? WHERE id = ? AND done_at IS NULL",
                (_iso(now or utcnow()), decision, request_id)).rowcount
        return self.get_refund_request(request_id) if n else None

    # --- «файл не пришёл» / замена без записи о покупке --------------------
    def add_delivery_request(self, user_id: int, kind: str, email: str,
                             product: str | None = None, now: datetime | None = None) -> int:
        assert kind in ("file", "swap")
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO delivery_requests(user_id, kind, email, product, created_at) "
                "VALUES (?, ?, ?, ?, ?)", (user_id, kind, email[:254], product, _iso(now or utcnow())))
        return cur.lastrowid

    def delivery_requests(self, *, open_only: bool = True) -> list[dict]:
        sql = "SELECT * FROM delivery_requests" + (" WHERE done_at IS NULL" if open_only else "")
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql + " ORDER BY id").fetchall()]

    def close_delivery_requests(self, user_id: int, now: datetime | None = None) -> int:
        with self._lock, self._conn:
            return self._conn.execute(
                "UPDATE delivery_requests SET done_at = ? WHERE user_id = ? AND done_at IS NULL",
                (_iso(now or utcnow()), user_id)).rowcount

    # --- вопросы без ответа (для сводки, без user_id) ---------------------
    def add_unanswered(self, question: str, now: datetime | None = None) -> None:
        with self._lock, self._conn:
            self._conn.execute("INSERT INTO unanswered_questions(question, created_at) VALUES (?, ?)",
                               (question[:500], _iso(now or utcnow())))

    def unanswered_between(self, start: datetime, end: datetime) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT question FROM unanswered_questions WHERE created_at >= ? AND created_at < ? "
                "ORDER BY id", (_iso(start), _iso(end))).fetchall()
        return [r["question"] for r in rows]

    def purge_unanswered(self, days: int, now: datetime | None = None) -> int:
        border = _iso((now or utcnow()) - timedelta(days=days))
        with self._lock, self._conn:
            return self._conn.execute("DELETE FROM unanswered_questions WHERE created_at < ?",
                                      (border,)).rowcount

    # --- счётчики для сводки ---------------------------------------------
    def count_between(self, what: str, start: datetime, end: datetime) -> int:
        queries = {
            "users": "SELECT COUNT(*) FROM users WHERE created_at >= ? AND created_at < ?",
            "purchases": "SELECT COUNT(*) FROM purchases WHERE source NOT IN ('swap', 'gift') "
                         "AND granted_at >= ? AND granted_at < ?",
            "swaps": "SELECT COUNT(*) FROM purchases WHERE source IN ('swap', 'gift') "
                     "AND granted_at >= ? AND granted_at < ?",
            "refunds": "SELECT COUNT(*) FROM refund_requests WHERE created_at >= ? AND created_at < ?",
            "crisis": "SELECT COUNT(*) FROM followup_state WHERE crisis_at >= ? AND crisis_at < ?",
        }
        with self._lock:
            return self._conn.execute(queries[what], (_iso(start), _iso(end))).fetchone()[0]

    # --- служебные значения ----------------------------------------------
    def get_meta(self, key: str) -> str | None:
        with self._lock:
            r = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return r["value"] if r else None

    def set_meta(self, key: str, value: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("INSERT INTO meta(key, value) VALUES (?, ?) "
                               "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))

    # --- удаление по запросу (/delete_me) --------------------------------
    def has_open_refund(self, user_id: int) -> bool:
        return bool(self.refund_requests(open_only=True, user_id=user_id))

    def delete_user(self, user_id: int) -> bool:
        """Удаляет данные пользователя. True — если что-то было удалено.

        Остаётся только то, что нужно для исполнения закона и решения владелицы:
        заявление о возврате без решения (до /refunded или /reject) и отметка no_sell (user_id + дата).
        """
        with self._lock, self._conn:
            n = 0
            n += self._conn.execute("DELETE FROM refund_requests WHERE user_id = ? "
                                    "AND done_at IS NOT NULL", (user_id,)).rowcount
            n += self._conn.execute("DELETE FROM delivery_requests WHERE user_id = ?",
                                    (user_id,)).rowcount
            n += self._conn.execute("DELETE FROM dialog_log WHERE user_id = ?", (user_id,)).rowcount
            n += self._conn.execute("DELETE FROM purchases WHERE user_id = ?", (user_id,)).rowcount
            n += self._conn.execute("DELETE FROM users WHERE user_id = ?", (user_id,)).rowcount
            n += self._conn.execute("DELETE FROM scheduled_messages WHERE user_id = ?",
                                    (user_id,)).rowcount
            n += self._conn.execute("DELETE FROM followup_state WHERE user_id = ?",
                                    (user_id,)).rowcount
        return n > 0
