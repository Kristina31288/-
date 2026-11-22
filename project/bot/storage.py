"""Хранилище SQLite (стандартная библиотека).

Что храним (минимум по 152-ФЗ):
- users: Telegram user_id, имя из профиля Telegram, дата согласия на обработку ПДн,
  согласие на рассылку (и его дата).
- purchases: что и когда выдано пользователю.
- dialog_log: вопросы к ИИ-помощнику и ответы, хранятся DIALOG_LOG_DAYS дней
  (для разбора спорных случаев). Тексты кризисных сообщений НЕ сохраняются —
  только отметка, что сработал кризисный сценарий.

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
        self._conn.commit()

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
                "SELECT user_id FROM users WHERE marketing_consent = 1 AND pd_consent_at IS NOT NULL"
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

    # --- удаление по запросу (/delete_me) --------------------------------
    def delete_user(self, user_id: int) -> bool:
        """Удаляет все данные пользователя. True — если что-то было удалено."""
        with self._lock, self._conn:
            n = 0
            n += self._conn.execute("DELETE FROM dialog_log WHERE user_id = ?", (user_id,)).rowcount
            n += self._conn.execute("DELETE FROM purchases WHERE user_id = ?", (user_id,)).rowcount
            n += self._conn.execute("DELETE FROM users WHERE user_id = ?", (user_id,)).rowcount
        return n > 0
