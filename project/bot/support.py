"""Сервисные сценарии Виктории: возврат, «файл не пришёл», «не подошло», жалоба, мат,
просьба о человеке. Фиксированные тексты, без ИИ и без сети — поэтому работают одинаково
с GigaChat и без него и тестируются напрямую.

Владелице ничего не передаётся «вживую». Заявления о возврате и заявки на выдачу файла
после проверки оплаты пишутся в базу и попадают в ежедневную сводку (summary.py).

Предмет продажи — PDF-файл, выдаётся сразу после оплаты. Возврат — только в законных
случаях (решение владелицы, пп. 3–4):
  «верните деньги» → Виктория спрашивает причину кнопками:
  - «Платёж прошёл дважды» → e-mail оплаты + дата платежей → заявление (double);
  - «Файл не пришёл или не открывается» → файл сразу выдаётся повторно (если покупка
    записана в боте) + как открыть PDF; кнопки «Открылось» / «Всё равно не открывается»;
    при втором → e-mail оплаты → заявление (file). Покупки в боте нет → сначала выдача
    файла по e-mail оплаты (заявка /give в сводке);
  - «Другая причина» → один раз предлагается замена разбора (для набора — подарок
    «Карта спокойного вечера»); клиент настаивает → e-mail оплаты → «Принимаю ваше
    заявление, его рассмотрят в течение 10 дней» → заявление (other).
  Виктория не обещает возврат, не спорит и не цитирует законы.

Отметка no_sell ставится ТОЛЬКО когда владелица отметила возврат выполненным
(/refunded <№> → resolve_refund). Подача заявления и отказ (/reject) её не ставят.

Состояние диалога (на каком шаге человек) хранится в памяти процесса: после перезапуска
бот просто начнёт сценарий заново (кнопки причины и «Открылось» работают и без него).
Сами заявления и no_sell — в SQLite.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

import catalog
import followup
import texts
from safety import detect_intent
from storage import Purchase, Storage
from textnorm import normalize

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-zА-Яа-я]{2,}")
_INSIST_RE = re.compile(
    r"\b(?:нет|не\s+хочу|не\s+надо|не\s+нужн\w*|деньг\w*|верн\w*|возврат\w*|настаива\w*|"
    r"вс[её]\s+равно|только\s+деньги|заявлени\w*)\b", re.IGNORECASE)
_MONEY_RE = re.compile(r"\b(?:деньг\w*|верн\w*|возврат\w*)", re.IGNORECASE)
_AGREE_SWAP_RE = re.compile(r"\b(?:да|давайте|замен\w*|хорошо|ок|согласн\w*|другой|подарок)\b",
                            re.IGNORECASE)
_CANCEL_RE = re.compile(r"\b(?:отмен\w*|передумал\w*|не\s+надо|не\s+нужно)\b", re.IGNORECASE)
# Покупки на Wildberries: возврат по правилам площадки, бот его не оформляет.
_WB_RE = re.compile(r"(?<!\w)(?:wb|вб|wildberries|в[аеи]й?лдберр?и\w*|в[ао]лдберр?и\w*)(?!\w)", re.IGNORECASE)

# Причина возврата из текста (по нормализованному тексту: нижний регистр, ё→е, без пунктуации).
_DOUBLE_RE = re.compile(
    r"\b(?:дважды|два\s+раза|2\s+раза|двойн\w*|дубл\w*|задвоил\w*|"
    r"повторн\w*\s+(?:списан\w*|оплат\w*|платеж\w*)|(?:две|2)\s+(?:оплат\w*|платеж\w*|списан\w*))")
_FILE_RE = re.compile(
    r"\bне\s+(?:при(?:шел|шло|шла|ходит)|открыва\w*|открыл\w*|открыт\w*|скачива\w*|скача\w*|"
    r"загружа\w*|загрузил\w*)\b|\bповрежд\w*|\bбит\w*\s+файл|\bнет\s+файл\w*|"
    r"\bне\s+получил\w*\s+(?:файл|разбор|набор|письм|pdf|пдф)")
_OTHER_RE = re.compile(
    r"\b(?:друг\w*|не\s+понрав\w*|не\s+подош\w*|не\s+помог\w*|передумал\w*|ерунд\w*|"
    r"бесполезн\w*|не\s+то\b|не\s+устраива\w*|дорог\w*)")
# Ответ на «Получилось открыть?»
_NOT_OPENED_RE = re.compile(
    r"\b(?:не\s+(?:открыва\w*|открыл\w*|открыт\w*|получ\w*|работа\w*|выход\w*|видно|вижу)|нет|"
    r"вс[её]\s+равно|опять|снова)\b")
_OPENED_RE = re.compile(r"\b(?:открыл\w*|открыва\w*|получилось|работает|да|ок|ok|спасибо|вижу)\b")
# Дата платежа для «платёж прошёл дважды» (по исходному тексту, без e-mail).
_DATE_RE = re.compile(
    r"\b\d{1,2}[./\-]\d{1,2}(?:[./\-]\d{2,4})?\b|"
    r"\b\d{1,2}\s+(?:янв|фев|мар|апр|ма[яй]|июн|июл|авг|сен|окт|ноя|дек)[а-яё]*|"
    r"\b(?:позавчера|вчера|сегодня)\b", re.IGNORECASE)
MAX_EMAIL_RETRIES = 1

# Кнопки сценария возврата (обрабатываются в handlers.py → Support.on_button).
CB_REASON = "rf:r:"            # rf:r:double | rf:r:file | rf:r:other
CB_FILE_OK = "rf:f:ok"
CB_FILE_BAD = "rf:f:bad"
REASONS = ("double", "file", "other")


def find_email(text: str) -> str | None:
    m = EMAIL_RE.search(text or "")
    return m.group(0).strip(".").lower() if m else None


def find_date(text: str) -> str | None:
    """Дата платежа со слов клиента («21.11», «21 ноября», «вчера») — как написано, до 40 символов."""
    m = _DATE_RE.search(EMAIL_RE.sub(" ", text or ""))
    return m.group(0).strip()[:40] if m else None


def guess_reason(text: str) -> str | None:
    """double | file — если причина видна из текста; иначе None (спросим кнопками)."""
    t = normalize(text)
    if _DOUBLE_RE.search(t):
        return "double"
    if _FILE_RE.search(t) or detect_intent(text, only=("file_missing",)).found:
        return "file"
    return None


def reason_rows() -> list[list[followup.Button]]:
    return [[followup.Button(texts.BTN_REFUND_DOUBLE, callback=CB_REASON + "double")],
            [followup.Button(texts.BTN_REFUND_FILE, callback=CB_REASON + "file")],
            [followup.Button(texts.BTN_REFUND_OTHER, callback=CB_REASON + "other")]]


def file_check_rows() -> list[list[followup.Button]]:
    return [[followup.Button(texts.BTN_FILE_OK, callback=CB_FILE_OK),
             followup.Button(texts.BTN_FILE_BAD, callback=CB_FILE_BAD)]]


def resolve_refund(db: Storage, request_id: int, decision: str,
                   now: datetime | None = None) -> dict | None:
    """Решение владелицы по заявлению: refunded (/refunded) или rejected (/reject).

    no_sell и отмена продающих сообщений — только после выполненного возврата.
    Возвращает закрытое заявление или None, если открытого заявления с таким № нет.
    """
    req = db.close_refund_request(request_id, decision, now)
    if req is not None and decision == "refunded":
        db.set_no_sell(req["user_id"], now)
        db.cancel_pending(req["user_id"], followup.SALES_KINDS, reason="no_sell")
    return req


@dataclass
class Reply:
    text: str
    rows: list[list[followup.Button]] = field(default_factory=list)
    resend_files: bool = False      # прислать заново файлы всех покупок из бота
    files_first: bool = False       # сначала файлы, потом текст с кнопками («Открылось?»)


@dataclass
class _Stage:
    # refund_reason | file_check | double_info | refund_offered | refund_email |
    # file_email | swap_email
    name: str
    retries: int = 0
    swap_rows: list = field(default_factory=list)
    reason: str = "other"           # для refund_email: основание заявления
    email: str | None = None        # для double_info: e-mail оплаты (если уже прислали)
    paid_on: str | None = None      # для double_info: дата платежей со слов клиента
    date_asked: bool = False        # для double_info: дату уже переспросили
    after_refund: bool = False      # file_email из сценария возврата


@dataclass(frozen=True)
class SwapTarget:
    purchase: Purchase | None = None
    choices: tuple[str, ...] = ()
    reason: str = ""                # почему замена недоступна (если purchase есть, а choices нет)
    gift: bool = False              # набор: вместо замены — подарок «Карта спокойного вечера»

    @property
    def ok(self) -> bool:
        return self.purchase is not None and (bool(self.choices) or self.gift)


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
        """Человек нажал кнопку замены или подарка — сценарий возврата закончен."""
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
        if intent in ("refund", "nofit", "complaint", "file_missing") and _WB_RE.search(text or ""):
            self._stage.pop(user_id, None)
            return Reply(texts.WB_REFUND)
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

    def on_button(self, user_id: int, data: str, no_sell: bool = False) -> Reply | None:
        """Кнопки сценария возврата (rf:…). Работают и без состояния в памяти."""
        if data.startswith(CB_REASON) and data[len(CB_REASON):] in REASONS:
            if self._has_open_refund(user_id):
                self._stage.pop(user_id, None)
                return Reply(texts.REFUND_ALREADY)
            return self._start_reason(user_id, data[len(CB_REASON):], "", no_sell)
        if data == CB_FILE_OK:
            self._stage.pop(user_id, None)
            return Reply(texts.REFUND_FILE_OK)
        if data == CB_FILE_BAD:
            if self._has_open_refund(user_id):
                self._stage.pop(user_id, None)
                return Reply(texts.REFUND_ALREADY)
            return self._ask_or_accept(user_id, None, "file")
        return None

    # --- возврат: причина ---------------------------------------------------
    def start_refund(self, user_id: int, text: str, no_sell: bool = False) -> Reply:
        if self._has_open_refund(user_id):
            self._stage.pop(user_id, None)
            return Reply(texts.REFUND_ALREADY)
        reason = guess_reason(text)
        if reason is not None:
            return self._start_reason(user_id, reason, text, no_sell)
        self._stage[user_id] = _Stage("refund_reason")
        return Reply(texts.REFUND_ASK_REASON, reason_rows())

    def _start_reason(self, user_id: int, reason: str, text: str, no_sell: bool) -> Reply:
        if reason == "double":
            return self._double(user_id, _Stage("double_info"), text)
        if reason == "file":
            return self._refund_file(user_id, text)
        return self._refund_other(user_id, text, no_sell)

    # --- ветка «платёж прошёл дважды» ---------------------------------------
    def _double(self, user_id: int, st: _Stage, text: str, in_stage: bool = False) -> Reply | None:
        """Нужны e-mail оплаты и дата платежей. Дату спрашиваем один раз — без неё тоже принимаем."""
        st.email = find_email(text) or st.email
        st.paid_on = find_date(text) or st.paid_on
        if st.email and (st.paid_on or st.date_asked):
            return self._accept_refund(user_id, st.email, "double", st.paid_on)
        self._stage[user_id] = st
        if st.email:
            st.date_asked = True
            return Reply(texts.REFUND_DOUBLE_ASK_DATE)
        if not in_stage:
            return Reply(texts.REFUND_DOUBLE_ASK)
        if _CANCEL_RE.search(text or ""):
            self._stage.pop(user_id, None)
            return Reply(texts.REFUND_CANCELLED)
        if st.retries < MAX_EMAIL_RETRIES:
            st.retries += 1
            return Reply(texts.EMAIL_AGAIN)
        self._stage.pop(user_id, None)          # разговор ушёл в сторону
        return None

    # --- ветка «файл не пришёл или не открывается» --------------------------
    def _refund_file(self, user_id: int, text: str) -> Reply:
        if self._has_purchases(user_id):
            self._stage[user_id] = _Stage("file_check")
            return Reply(texts.REFUND_FILE_RESEND, file_check_rows(), resend_files=True,
                         files_first=True)
        # покупки в боте нет: сначала выдаём файл по e-mail оплаты (заявка /give в сводке)
        email = find_email(text)
        if email:
            return self._file_request(user_id, email, after_refund=True)
        self._stage[user_id] = _Stage("file_email", after_refund=True)
        return Reply(texts.REFUND_FILE_NO_RECORD)

    # --- ветка «другая причина» ---------------------------------------------
    def _refund_other(self, user_id: int, text: str, no_sell: bool) -> Reply:
        email = find_email(text)
        if no_sell or user_id in self._swap_offered:
            return self._ask_or_accept(user_id, email, "other")
        self._swap_offered.add(user_id)
        target = self.swap_target(user_id)
        if target.ok:
            rows = self._swap_rows(target)
            self._stage[user_id] = _Stage("refund_offered", swap_rows=rows)
            return Reply(texts.REFUND_OTHER_GIFT if target.gift else texts.REFUND_OTHER_SWAP, rows)
        if target.purchase is None and not self._has_purchases(user_id):
            # покупки в боте нет (файл пришёл на e-mail) — замену оформляем по e-mail оплаты
            self._stage[user_id] = _Stage("refund_offered")
            return Reply(texts.REFUND_OTHER_SWAP_NO_RECORD)
        # замена невозможна (уже была, прошло 14 дней, все разборы есть) — сразу к заявлению
        return self._ask_or_accept(user_id, email, "other")

    def _ask_or_accept(self, user_id: int, email: str | None, reason: str) -> Reply:
        if email:
            return self._accept_refund(user_id, email, reason)
        self._stage[user_id] = _Stage("refund_email", reason=reason)
        return Reply(texts.REFUND_FILE_ASK_EMAIL if reason == "file" else texts.REFUND_OTHER_ASK_EMAIL)

    def _accept_refund(self, user_id: int, email: str, reason: str,
                       paid_on: str | None = None) -> Reply:
        """Заявление в базу. no_sell НЕ ставим: только после /refunded."""
        self._stage.pop(user_id, None)
        if self.db is not None:
            self.db.add_refund_request(user_id, email, self._last_paid_purchase(user_id),
                                       reason=reason, paid_on=paid_on)
        return Reply(texts.REFUND_DOUBLE_ACCEPTED if reason == "double" else texts.REFUND_ACCEPTED)

    # --- продолжение начатого сценария ----------------------------------
    def _continue(self, user_id: int, st: _Stage, text: str, no_sell: bool) -> Reply | None:
        email = find_email(text)
        norm = normalize(text)
        if st.name == "refund_reason":
            reason = guess_reason(text)
            if reason is None and (_OTHER_RE.search(norm) or detect_intent(
                    text, only=("refund", "nofit", "complaint")).found):
                reason = "other"
            if reason is None:
                self._stage.pop(user_id, None)          # заговорили о другом — сценарий закрыт
                return None
            return self._start_reason(user_id, reason, text, no_sell)
        if st.name == "file_check":
            if _NOT_OPENED_RE.search(norm) or detect_intent(text, only=("refund",)).found:
                return self._ask_or_accept(user_id, email, "file")
            if _OPENED_RE.search(norm):
                self._stage.pop(user_id, None)
                return Reply(texts.REFUND_FILE_OK)
            self._stage.pop(user_id, None)
            return None
        if st.name == "double_info":
            return self._double(user_id, st, text, in_stage=True)
        if st.name == "refund_offered":
            wants_money = detect_intent(text, only=("refund",)).found or _MONEY_RE.search(text or "")
            if not wants_money and st.swap_rows and _AGREE_SWAP_RE.search(text or ""):
                return Reply(texts.REFUND_PICK_SWAP, st.swap_rows)
            if wants_money or _INSIST_RE.search(text or ""):
                return self._ask_or_accept(user_id, email, "other")
            if not st.swap_rows and email:          # нет записи о покупке: замена по e-mail
                return self._swap_request(user_id, email, text)
            self._stage.pop(user_id, None)          # заговорили о другом — сценарий закрыт
            return None
        if st.name in ("refund_email", "file_email", "swap_email"):
            if email:
                if st.name == "refund_email":
                    return self._accept_refund(user_id, email, st.reason)
                if st.name == "file_email":
                    return self._file_request(user_id, email, st.after_refund)
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

    def _file_request(self, user_id: int, email: str, after_refund: bool = False) -> Reply:
        self._stage.pop(user_id, None)
        if self.db is not None:
            self.db.add_delivery_request(user_id, "file", email)
        return Reply(texts.FILE_REQUEST_ACCEPTED
                     + ("\n\n" + texts.REFUND_FILE_REQUEST_NOTE if after_refund else ""))

    # --- не подошло / замена --------------------------------------------
    def _nofit(self, user_id: int, text: str, no_sell: bool) -> Reply:
        if no_sell:
            return Reply(texts.SWAP_AFTER_REFUND)
        target = self.swap_target(user_id)
        if target.ok:
            return Reply(texts.GIFT_OFFER if target.gift else texts.SWAP_OFFER, self._swap_rows(target))
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
    def _has_open_refund(self, user_id: int) -> bool:
        return self.db is not None and self.db.has_open_refund(user_id)

    def _has_purchases(self, user_id: int) -> bool:
        return self.db is not None and bool(self.db.list_purchases(user_id))

    def _last_paid_purchase(self, user_id: int) -> Purchase | None:
        if self.db is None:
            return None
        paid = [p for p in self.db.list_purchases(user_id) if p.source not in followup.NOT_PAID_SOURCES]
        return paid[-1] if paid else None

    def swap_target(self, user_id: int, now: datetime | None = None) -> SwapTarget:
        """Последняя оплаченная покупка и что можно прислать вместо (замена или подарок)."""
        last = self._last_paid_purchase(user_id)
        if last is None:
            return SwapTarget()
        if last.product == "nabor":
            if followup.gift_used(self.db, user_id, last.id):
                return SwapTarget(last, reason="подарок по этой покупке уже отправлен")
            if followup.swap_window_over(last, now):
                return SwapTarget(last, reason=texts.SWAP_EXPIRED_REASON)
            return SwapTarget(last, gift=True)
        if followup.swap_used(self.db, user_id, last.id):
            return SwapTarget(last, reason="замена по этой покупке уже была")
        if followup.swap_window_over(last, now):
            return SwapTarget(last, reason=texts.SWAP_EXPIRED_REASON)
        owned = followup.owned_guides(self.db, user_id)
        choices = tuple(k for k in followup.GUIDE_KEYS if k not in owned)
        if not choices:
            return SwapTarget(last, reason="у вас уже есть все разборы")
        return SwapTarget(last, choices)

    @staticmethod
    def _swap_rows(target: SwapTarget) -> list[list[followup.Button]]:
        if target.gift:
            return followup.gift_rows(target.purchase.id)
        return [[followup.Button(followup.SHORT_TITLES[k], callback=f"fu:swap:{target.purchase.id}:{k}")]
                for k in target.choices]
