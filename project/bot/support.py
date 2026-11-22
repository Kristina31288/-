"""Сервисные сценарии Виктории: возврат, «файл не пришёл», «не подошло», жалоба, мат,
просьба о человеке. Фиксированные тексты, без ИИ и без сети — поэтому работают одинаково
с GigaChat и без него и тестируются напрямую.

Владелице ничего не передаётся «вживую». Заявки (возврат, выдача файла после проверки
оплаты) пишутся в базу и попадают в еженедельную сводку (summary.py).

Возврат (решение владелицы, п. 4):
  1) клиент просит вернуть деньги → Виктория коротко признаёт и ОДИН раз предлагает замену;
  2) клиент настаивает → не спорит, просит e-mail, на который была оплата;
  3) e-mail получен → заявка в refund_requests, отметка no_sell навсегда, продающие
     сообщения отменяются, клиенту — нейтральный текст про срок (зависит от банка).

Состояние диалога (на каком шаге человек) хранится в памяти процесса: после перезапуска
бот просто начнёт сценарий заново. Сами заявки и no_sell — в SQLite.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import catalog
import followup
import texts
from safety import detect_intent
from storage import Purchase, Storage

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-zА-Яа-я]{2,}")
_INSIST_RE = re.compile(
    r"\b(?:нет|не\s+хочу|не\s+надо|не\s+нужн\w*|деньг\w*|верн\w*|возврат\w*|настаива\w*|"
    r"вс[её]\s+равно|только\s+деньги)\b", re.IGNORECASE)
_MONEY_RE = re.compile(r"\b(?:деньг\w*|верн\w*|возврат\w*)", re.IGNORECASE)
_AGREE_SWAP_RE = re.compile(r"\b(?:да|давайте|замен\w*|хорошо|ок|согласн\w*|другой)\b", re.IGNORECASE)
_CANCEL_RE = re.compile(r"\b(?:отмен\w*|передумал\w*|не\s+надо|не\s+нужно)\b", re.IGNORECASE)
MAX_EMAIL_RETRIES = 1


def find_email(text: str) -> str | None:
    m = EMAIL_RE.search(text or "")
    return m.group(0).strip(".").lower() if m else None


@dataclass
class Reply:
    text: str
    rows: list[list[followup.Button]] = field(default_factory=list)
    resend_files: bool = False      # прислать заново файлы всех покупок из бота


@dataclass
class _Stage:
    name: str                       # refund_offered | refund_email | file_email | swap_email
    retries: int = 0
    swap_rows: list = field(default_factory=list)


@dataclass(frozen=True)
class SwapTarget:
    purchase: Purchase | None = None
    choices: tuple[str, ...] = ()
    reason: str = ""                # почему замена недоступна (если purchase есть, а choices нет)

    @property
    def ok(self) -> bool:
        return self.purchase is not None and bool(self.choices)


class Support:
    def __init__(self, db: Storage | None) -> None:
        self.db = db
        self._stage: dict[int, _Stage] = {}
        self._swap_offered: set[int] = set()     # замену при возврате предлагаем один раз

    def forget(self, user_id: int) -> None:
        self._stage.pop(user_id, None)
        self._swap_offered.discard(user_id)

    def stage(self, user_id: int) -> str | None:
        st = self._stage.get(user_id)
        return st.name if st else None

    def swap_chosen(self, user_id: int) -> None:
        """Человек нажал кнопку замены — сценарий возврата закончен."""
        self._stage.pop(user_id, None)

    # --- вход ------------------------------------------------------------
    def handle(self, user_id: int, text: str, no_sell: bool = False) -> Reply | None:
        """Ответ по сценарию или None — тогда вопрос уходит ИИ/FAQ."""
        st = self._stage.get(user_id)
        if st is not None:
            r = self._continue(user_id, st, text, no_sell)
            if r is not None:
                return r
        intent = detect_intent(text).intent
        if intent == "refund":
            return self.start_refund(user_id, text, no_sell)
        if intent == "file_missing":
            return self._file_missing(user_id, text)
        if intent == "nofit":
            return self._nofit(user_id, text, no_sell)
        if intent == "complaint":
            return Reply(texts.COMPLAINT_REPLY)
        if intent == "human":
            return Reply(texts.HUMAN_REPLY)
        if intent == "profanity":
            return Reply(texts.PROFANITY_REPLY)
        return None

    # --- возврат ---------------------------------------------------------
    def start_refund(self, user_id: int, text: str, no_sell: bool = False) -> Reply:
        if self.db is not None and self.db.has_open_refund(user_id):
            self._stage.pop(user_id, None)
            return Reply(texts.REFUND_ALREADY)
        email = find_email(text)
        if no_sell or user_id in self._swap_offered:
            return self._ask_or_accept(user_id, email)
        self._swap_offered.add(user_id)
        target = self.swap_target(user_id)
        if target.ok:
            rows = self._swap_rows(target)
            self._stage[user_id] = _Stage("refund_offered", swap_rows=rows)
            return Reply(texts.REFUND_OFFER_SWAP, rows)
        if target.purchase is None and not self._has_purchases(user_id):
            # покупки в боте нет (файл пришёл на e-mail) — замену оформляем по e-mail оплаты
            self._stage[user_id] = _Stage("refund_offered")
            return Reply(texts.REFUND_OFFER_SWAP_NO_RECORD)
        # замена невозможна (набор, замена уже была) — сразу к заявке, без спора
        return self._ask_or_accept(user_id, email)

    def _ask_or_accept(self, user_id: int, email: str | None) -> Reply:
        if email:
            return self._accept_refund(user_id, email)
        self._stage[user_id] = _Stage("refund_email")
        return Reply(texts.REFUND_ASK_EMAIL)

    def _accept_refund(self, user_id: int, email: str) -> Reply:
        self._stage.pop(user_id, None)
        if self.db is not None:
            self.db.add_refund_request(user_id, email, self._last_paid_purchase(user_id))
            self.db.set_no_sell(user_id)
            self.db.cancel_pending(user_id, followup.SALES_KINDS, reason="no_sell")
        return Reply(texts.REFUND_ACCEPTED)

    # --- продолжение начатого сценария ----------------------------------
    def _continue(self, user_id: int, st: _Stage, text: str, no_sell: bool) -> Reply | None:
        email = find_email(text)
        if st.name == "refund_offered":
            wants_money = detect_intent(text, only=("refund",)).found or _MONEY_RE.search(text or "")
            if not wants_money and st.swap_rows and _AGREE_SWAP_RE.search(text or ""):
                return Reply(texts.REFUND_PICK_SWAP, st.swap_rows)
            if wants_money or _INSIST_RE.search(text or ""):
                return self._ask_or_accept(user_id, email)
            if not st.swap_rows and email:          # нет записи о покупке: замена по e-mail
                return self._swap_request(user_id, email, text)
            self._stage.pop(user_id, None)          # заговорили о другом — сценарий закрыт
            return None
        if st.name in ("refund_email", "file_email", "swap_email"):
            if email:
                if st.name == "refund_email":
                    return self._accept_refund(user_id, email)
                if st.name == "file_email":
                    return self._file_request(user_id, email)
                return self._swap_request(user_id, email, text)
            if _CANCEL_RE.search(text or ""):
                self._stage.pop(user_id, None)
                return Reply(texts.REFUND_CANCELLED if st.name == "refund_email" else texts.CANCELLED)
            if st.retries < MAX_EMAIL_RETRIES:
                st.retries += 1
                return Reply(texts.EMAIL_AGAIN)
            self._stage.pop(user_id, None)
            return None
        self._stage.pop(user_id, None)
        return None

    # --- файл не пришёл -------------------------------------------------
    def _file_missing(self, user_id: int, text: str) -> Reply:
        if self._has_purchases(user_id):
            return Reply(texts.FILE_RESEND, resend_files=True)
        email = find_email(text)
        if email:
            return self._file_request(user_id, email)
        self._stage[user_id] = _Stage("file_email")
        return Reply(texts.FILE_HELP)

    def _file_request(self, user_id: int, email: str) -> Reply:
        self._stage.pop(user_id, None)
        if self.db is not None:
            self.db.add_delivery_request(user_id, "file", email)
        return Reply(texts.FILE_REQUEST_ACCEPTED)

    # --- не подошло / замена --------------------------------------------
    def _nofit(self, user_id: int, text: str, no_sell: bool) -> Reply:
        if no_sell:
            return Reply(texts.SWAP_AFTER_REFUND)
        target = self.swap_target(user_id)
        if target.ok:
            return Reply(texts.SWAP_OFFER, self._swap_rows(target))
        if target.purchase is not None or self._has_purchases(user_id):
            return Reply(texts.SWAP_NOT_AVAILABLE.format(reason=target.reason or "замена уже была"))
        email = find_email(text)
        if email:
            return self._swap_request(user_id, email, text)
        self._stage[user_id] = _Stage("swap_email")
        return Reply(texts.SWAP_OFFER_NO_RECORD)

    def _swap_request(self, user_id: int, email: str, text: str) -> Reply:
        self._stage.pop(user_id, None)
        if self.db is not None:
            self.db.add_delivery_request(user_id, "swap", email,
                                         catalog.guess_guide(EMAIL_RE.sub(" ", text or "")))
        return Reply(texts.SWAP_REQUEST_ACCEPTED)

    # --- покупки ----------------------------------------------------------
    def _has_purchases(self, user_id: int) -> bool:
        return self.db is not None and bool(self.db.list_purchases(user_id))

    def _last_paid_purchase(self, user_id: int) -> Purchase | None:
        if self.db is None:
            return None
        paid = [p for p in self.db.list_purchases(user_id) if p.source != followup.SWAP_SOURCE]
        return paid[-1] if paid else None

    def swap_target(self, user_id: int) -> SwapTarget:
        """Последняя оплаченная покупка одного разбора и какие разборы можно прислать вместо."""
        last = self._last_paid_purchase(user_id)
        if last is None:
            return SwapTarget()
        if last.product == "nabor":
            return SwapTarget(last, reason="в наборе уже есть все три разбора")
        if followup.swap_used(self.db, user_id, last.id):
            return SwapTarget(last, reason="замена по этой покупке уже была")
        owned = followup.owned_guides(self.db, user_id)
        choices = tuple(k for k in followup.GUIDE_KEYS if k not in owned)
        if not choices:
            return SwapTarget(last, reason="у вас уже есть все разборы")
        return SwapTarget(last, choices)

    @staticmethod
    def _swap_rows(target: SwapTarget) -> list[list[followup.Button]]:
        return [[followup.Button(followup.SHORT_TITLES[k], callback=f"fu:swap:{target.purchase.id}:{k}")]
                for k in target.choices]
