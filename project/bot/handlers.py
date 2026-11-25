"""Обработчики Telegram (aiogram 3).

С клиентами общается только ИИ-помощница «Виктория». Владелица с клиентами не переписывается:
команд «ответить пользователю» и кнопок «Написать автору» / «Консультация» нет. Всё, что
требует её действий, приходит в ежедневной сводке (summary.py, 10:00 МСК).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    User as TgUser,
)

import catalog
import followup
import safety
import summary
import support
import texts
from config import Config
from dialog import DialogContext, Decision, Kind, process_text
from storage import Storage

log = logging.getLogger(__name__)


# Кнопки старого меню, которые могли остаться на клавиатуре у пользователей.
LEGACY_BUTTONS = ("Консультация", "Написать автору", "Спросить ИИ-помощника")


@dataclass
class Deps:
    config: Config
    storage: Storage
    dialog: DialogContext


# --- клавиатуры -------------------------------------------------------------
def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=texts.BTN_GUIDES), KeyboardButton(text=texts.BTN_ASK)],
        ],
        resize_keyboard=True,
    )


def consent_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=texts.BTN_CONSENT, callback_data="consent:yes")]])


def marketing_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=texts.BTN_MARKETING_YES, callback_data="news:yes"),
        InlineKeyboardButton(text=texts.BTN_MARKETING_NO, callback_data="news:no"),
    ]])


def delete_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=texts.BTN_DELETE_YES, callback_data="delete:yes"),
        InlineKeyboardButton(text=texts.BTN_DELETE_NO, callback_data="delete:no"),
    ]])


def pay_kb(link: str, price_text: str) -> InlineKeyboardMarkup | None:
    if not link:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=texts.PAY_BUTTON.format(price=price_text), url=link)]])


def to_markup(rows: list[list[followup.Button]]) -> InlineKeyboardMarkup | None:
    if not rows:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=b.text, url=b.url) if b.url
         else InlineKeyboardButton(text=b.text, callback_data=b.callback) for b in row]
        for row in rows])


def user_label(u: TgUser | None) -> str:
    if u is None:
        return "?"
    name = u.first_name or ""
    uname = f" @{u.username}" if u.username else ""
    return f"{name}{uname} (id {u.id})"


# --- служебные функции ------------------------------------------------------
async def notify_owner(bot: Bot, config: Config, text: str) -> None:
    try:
        await bot.send_message(config.owner_id, safety.truncate(text, 4000))
    except TelegramAPIError:
        log.exception("Не удалось уведомить владелицу. Она нажала /start в боте?")


async def send_product_files(bot: Bot, config: Config, user_id: int, product: catalog.Product) -> list[str]:
    """Отправляет PDF продукта. Возвращает список отсутствующих файлов."""
    missing: list[str] = []
    for path in catalog.files_for(product, config):
        if not path.is_file():
            missing.append(path.name)
            continue
        await bot.send_document(user_id, FSInputFile(path))
    return missing


async def resend_purchases(bot: Bot, config: Config, db: Storage, user_id: int) -> list[str]:
    """«Файл не пришёл»: заново присылает PDF всех покупок из бота (без повторов)."""
    paths: list[Path] = []
    for p in db.list_purchases(user_id):
        if p.product == catalog.GIFT_EVENING_KEY:
            candidates = [Path(config.files_dir) / config.gift_file]
        elif p.product in catalog.PRODUCTS:
            candidates = catalog.files_for(catalog.PRODUCTS[p.product], config)
        else:
            continue
        paths += [c for c in candidates if c not in paths]
    missing = []
    for path in paths:
        if path.is_file():
            await bot.send_document(user_id, FSInputFile(path))
        else:
            missing.append(path.name)
    return missing


async def send_outgoing(bot: Bot, config: Config, user_id: int,
                        outs: list[followup.Outgoing]) -> None:
    """Отправляет сообщения, собранные в followup.py. Ошибки Telegram пробрасываются."""
    for o in outs:
        if o.send_lead_magnet:
            lead = Path(config.files_dir) / config.lead_magnet_file
            if lead.is_file():
                await bot.send_document(user_id, FSInputFile(lead))
        await bot.send_message(user_id, o.text, reply_markup=to_markup(o.rows))


def build_router(deps: Deps) -> Router:
    router = Router(name="main")
    cfg, db, dctx = deps.config, deps.storage, deps.dialog

    def is_owner(m: Message) -> bool:
        return m.from_user is not None and m.from_user.id == cfg.owner_id

    async def resend_files(bot: Bot, uid: int) -> None:
        missing = await resend_purchases(bot, cfg, db, uid)
        if missing:
            log.warning("Повторная выдача: нет файлов %s", ", ".join(missing))

    async def apply_decision(message: Message, d: Decision) -> None:
        uid = message.from_user.id
        markup = to_markup(d.rows) if d.rows else main_menu()
        if d.resend_files and d.files_first:
            await resend_files(message.bot, uid)
        await message.answer(d.reply, reply_markup=markup)
        if d.kind is Kind.CRISIS:
            # отметка без текста: продающие сообщения цепочки — стоп на 7 дней
            followup.on_crisis(db, uid)
        if d.resend_files and not d.files_first:
            await resend_files(message.bot, uid)
        if cfg.log_dialogs and db.has_consent(uid):
            if d.kind is Kind.CRISIS:
                db.log_dialog(uid, "event", "сработал кризисный сценарий (текст не сохраняется)")
            elif d.log_user_text:
                db.log_dialog(uid, "user", message.text or "")
                db.log_dialog(uid, "bot", d.reply)
        if d.notify_owner:      # только кризис и только при NOTIFY_OWNER_ON_CRISIS=1
            await notify_owner(message.bot, cfg,
                               f"[{d.owner_note}] {user_label(message.from_user)}:\n{message.text}")

    # --- /start и согласие ---------------------------------------------------
    @router.message(CommandStart())
    async def start(message: Message, state: FSMContext) -> None:
        await state.clear()
        db.clear_blocked(message.from_user.id)   # вернулся после блокировки
        if db.has_consent(message.from_user.id):
            await message.answer(texts.MENU_HINT, reply_markup=main_menu())
            return
        dctx.introduced.add(message.from_user.id)
        await message.answer(
            texts.WELCOME.format(intro=texts.victoria_intro(), policy=cfg.policy_url),
            reply_markup=consent_kb(),
        )

    @router.callback_query(F.data == "consent:yes")
    async def consent(cb: CallbackQuery) -> None:
        u = cb.from_user
        db.set_pd_consent(u.id, u.first_name)
        await cb.answer()
        if isinstance(cb.message, Message):
            await cb.message.edit_reply_markup(reply_markup=None)
        await cb.bot.send_message(u.id, texts.CONSENT_THANKS)
        lead = Path(cfg.files_dir) / cfg.lead_magnet_file
        if lead.is_file():
            await cb.bot.send_document(u.id, FSInputFile(lead))
            # цепочка советов; уйдёт только при согласии на рассылку (проверка при отправке)
            followup.start_chain(db, u.id)
        else:
            await cb.bot.send_message(u.id, texts.LEAD_MAGNET_MISSING)
            await notify_owner(cb.bot, cfg, f"Нет файла лид-магнита {lead}. Пользователь {user_label(u)}.")
        await cb.bot.send_message(u.id, texts.MARKETING_QUESTION, reply_markup=marketing_kb())
        await cb.bot.send_message(u.id, texts.MENU_HINT, reply_markup=main_menu())

    @router.callback_query(F.data.in_({"news:yes", "news:no"}))
    async def news_choice(cb: CallbackQuery) -> None:
        yes = cb.data == "news:yes"
        if db.has_consent(cb.from_user.id):
            db.set_marketing_consent(cb.from_user.id, yes)
        await cb.answer()
        if isinstance(cb.message, Message):
            await cb.message.edit_text(texts.MARKETING_ON if yes else texts.MARKETING_OFF)

    @router.message(Command("news_on", "news_off"))
    async def news_cmd(message: Message, command: CommandObject) -> None:
        if not db.has_consent(message.from_user.id):
            await message.answer(texts.NEED_CONSENT)
            return
        yes = command.command == "news_on"
        db.set_marketing_consent(message.from_user.id, yes)
        await message.answer(texts.MARKETING_ON if yes else texts.MARKETING_OFF)

    # --- /delete_me ------------------------------------------------------------
    @router.message(Command("delete_me"))
    async def delete_me(message: Message, state: FSMContext) -> None:
        await state.clear()
        body = texts.DELETE_CONFIRM
        if db.refund_requests(user_id=message.from_user.id):
            body += "\n" + texts.DELETE_REFUND_NOTE
        await message.answer(body, reply_markup=delete_kb())

    @router.callback_query(F.data.in_({"delete:yes", "delete:no"}))
    async def delete_choice(cb: CallbackQuery, state: FSMContext) -> None:
        await cb.answer()
        if cb.data == "delete:no":
            if isinstance(cb.message, Message):
                await cb.message.edit_text(texts.CANCELLED)
            return
        removed = db.delete_user(cb.from_user.id)
        dctx.forget(cb.from_user.id)
        await state.clear()
        if isinstance(cb.message, Message):
            await cb.message.edit_text(texts.DELETED if removed else texts.NOTHING_TO_DELETE)

    # --- справка и отмена --------------------------------------------------------
    @router.message(Command("help"))
    async def help_cmd(message: Message) -> None:
        await message.answer(texts.HELP + ("\n\n" + texts.OWNER_HELP if is_owner(message) else ""))

    @router.message(Command("cancel"))
    async def cancel(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer(texts.CANCELLED, reply_markup=main_menu())

    @router.message(Command("menu"))
    async def menu_cmd(message: Message, state: FSMContext) -> None:
        await state.clear()
        if not db.has_consent(message.from_user.id):
            await message.answer(texts.NEED_CONSENT)
            return
        await message.answer(texts.MENU_HINT, reply_markup=main_menu())

    # --- автоцепочка и «Как прошла неделя?» (followup.py) ----------------------
    def sales_paused(uid: int) -> bool:
        return dctx.crisis_cooldown.active(uid) or followup.sales_paused(db, uid)

    def no_sell(uid: int) -> bool:
        return db.is_no_sell(uid)

    def sell_blocked(uid: int) -> bool:
        # no_sell навсегда или пауза, пока заявление о возврате без решения
        return db.sales_blocked(uid)

    async def do_stop_tips(bot: Bot, uid: int) -> None:
        if db.get_user(uid) is not None:
            followup.stop_tips(db, uid)
        await bot.send_message(uid, texts.TIPS_STOPPED)

    @router.message(Command("stop_tips"))
    async def stop_tips_cmd(message: Message) -> None:
        await do_stop_tips(message.bot, message.from_user.id)

    @router.callback_query(F.data == "fu:stop")
    async def stop_tips_cb(cb: CallbackQuery) -> None:
        await cb.answer()
        if isinstance(cb.message, Message):
            await cb.message.edit_reply_markup(reply_markup=None)
        await do_stop_tips(cb.bot, cb.from_user.id)

    @router.callback_query(F.data.startswith("fu:sit:"))
    async def situation(cb: CallbackQuery) -> None:
        await cb.answer()
        uid, key = cb.from_user.id, cb.data.split(":", 2)[2]
        if key not in followup.GUIDE_KEYS:
            return
        if not db.has_consent(uid):
            await cb.bot.send_message(uid, texts.NEED_CONSENT)
            return
        if sales_paused(uid):
            await cb.bot.send_message(uid, safety.CRISIS_REPLY)
            return
        db.set_chosen_product(uid, key)
        await send_outgoing(cb.bot, cfg, uid,
                            followup.build_card(cfg, key, followup.owned_guides(db, uid),
                                                sell=not sell_blocked(uid)))

    @router.callback_query(F.data.startswith("fu:wk:"))
    async def week_answer(cb: CallbackQuery) -> None:
        await cb.answer()
        parts = cb.data.split(":")
        if len(parts) != 4 or parts[2] not in followup.WEEK_ANSWERS or not parts[3].isdigit():
            return
        uid, answer, pid = cb.from_user.id, parts[2], int(parts[3])
        if not db.has_consent(uid):
            await cb.bot.send_message(uid, texts.NEED_CONSENT)
            return
        if isinstance(cb.message, Message):
            await cb.message.edit_reply_markup(reply_markup=None)
        if sales_paused(uid) and answer != "nofit":
            reply = texts.WEEK_CALM if answer == "calm" else safety.CRISIS_REPLY
            await cb.bot.send_message(uid, reply)
            return
        await send_outgoing(cb.bot, cfg, uid, followup.build_week_answer(
            db, cfg, uid, answer, pid, sell=not sell_blocked(uid)))

    @router.callback_query(F.data.startswith("fu:swap:"))
    async def swap(cb: CallbackQuery) -> None:
        await cb.answer()
        parts = cb.data.split(":")
        if len(parts) != 4 or not parts[2].isdigit():
            return
        uid, pid, key = cb.from_user.id, int(parts[2]), parts[3]
        if not db.has_consent(uid):
            await cb.bot.send_message(uid, texts.NEED_CONSENT)
            return
        dctx.support.swap_chosen(uid)
        if no_sell(uid):
            await cb.bot.send_message(uid, texts.SWAP_AFTER_REFUND)
            return
        verdict = followup.check_swap(db, uid, pid, key)
        if not verdict.ok:
            await cb.bot.send_message(uid, verdict.reply)
            return
        product = catalog.PRODUCTS[key]
        paths = catalog.files_for(product, cfg)
        missing = [p.name for p in paths if not p.is_file()]
        if missing:
            await cb.bot.send_message(uid, texts.SWAP_FILE_LATER)
            await notify_owner(cb.bot, cfg, f"Замена разбора: нет файла {', '.join(missing)} в files/. "
                                            f"Положите файл — пользователь {user_label(cb.from_user)} "
                                            f"сможет нажать кнопку замены снова.")
            return
        if not followup.record_swap(db, uid, pid, key):
            await cb.bot.send_message(uid, texts.SWAP_ALREADY)
            return
        if isinstance(cb.message, Message):
            await cb.message.edit_reply_markup(reply_markup=None)
        await cb.bot.send_message(uid, texts.SWAP_DONE.format(title=product.title))
        await send_product_files(cb.bot, cfg, uid, product)

    # --- подарок вместо замены для набора («Карта спокойного вечера») ------------
    @router.callback_query(F.data.startswith("fu:gift:"))
    async def gift(cb: CallbackQuery) -> None:
        await cb.answer()
        parts = cb.data.split(":")
        if len(parts) != 3 or not parts[2].isdigit():
            return
        uid, pid = cb.from_user.id, int(parts[2])
        if not db.has_consent(uid):
            await cb.bot.send_message(uid, texts.NEED_CONSENT)
            return
        dctx.support.swap_chosen(uid)
        if no_sell(uid):
            await cb.bot.send_message(uid, texts.SWAP_AFTER_REFUND)
            return
        verdict = followup.check_gift(db, uid, pid)
        if not verdict.ok:
            await cb.bot.send_message(uid, verdict.reply)
            return
        path = Path(cfg.files_dir) / cfg.gift_file
        if not path.is_file():
            await cb.bot.send_message(uid, texts.GIFT_FILE_LATER)
            await notify_owner(cb.bot, cfg, f"Подарок «{catalog.GIFT_EVENING_TITLE}»: нет файла "
                                            f"{path.name} в files/. Положите файл.")
            return
        if not followup.record_gift(db, uid, pid):
            await cb.bot.send_message(uid, texts.GIFT_ALREADY)
            return
        if isinstance(cb.message, Message):
            await cb.message.edit_reply_markup(reply_markup=None)
        await cb.bot.send_message(uid, texts.GIFT_SENT)
        await cb.bot.send_document(uid, FSInputFile(path))

    # --- кнопки сценария возврата (support.py) -----------------------------------
    @router.callback_query(F.data.startswith("rf:"))
    async def refund_button(cb: CallbackQuery) -> None:
        await cb.answer()
        uid = cb.from_user.id
        if not db.has_consent(uid):
            await cb.bot.send_message(uid, texts.NEED_CONSENT)
            return
        r = dctx.support.on_button(uid, cb.data, no_sell=no_sell(uid))
        if r is None:
            return
        if isinstance(cb.message, Message):
            await cb.message.edit_reply_markup(reply_markup=None)
        if r.resend_files and r.files_first:
            await resend_files(cb.bot, uid)
        await cb.bot.send_message(uid, r.text, reply_markup=to_markup(r.rows))
        if r.resend_files and not r.files_first:
            await resend_files(cb.bot, uid)

    # --- команды владелицы -----------------------------------------------------
    @router.message(Command("give"))
    async def give(message: Message, command: CommandObject) -> None:
        if not is_owner(message):
            return  # молча игнорируем: не раскрываем, что команда существует
        parts = (command.args or "").split()
        if len(parts) != 2 or not parts[0].isdigit():
            await message.answer("Формат: /give <user_id> <telefon|podrostok|uroki|nabor>")
            return
        user_id, product = int(parts[0]), catalog.resolve_product(parts[1])
        if product is None:
            await message.answer("Не знаю такой продукт. Варианты: telefon, podrostok, uroki, nabor (или 1, 2, 3, набор).")
            return
        if db.get_user(user_id) is None:
            await message.answer("Этот пользователь не запускал бота (или удалил данные). "
                                 "Отправьте файл по e-mail или попросите нажать /start.")
            return
        try:
            await message.bot.send_message(user_id, texts.DELIVERY_TEXT.format(title=product.title))
            missing = await send_product_files(message.bot, cfg, user_id, product)
        except TelegramAPIError as e:
            await message.answer(f"Не удалось отправить: {e.message}. Возможно, пользователь заблокировал бота.")
            return
        if missing:
            await message.answer("Нет файлов в папке files/: " + ", ".join(missing) + ". Покупка не записана.")
            return
        db.add_purchase(user_id, product.key, "manual")
        db.close_delivery_requests(user_id)
        followup.on_purchase(db, user_id, db.last_purchase_id(user_id))
        await message.bot.send_message(user_id, texts.FEEDBACK_ASK)
        await message.answer(f"Выдано: {product.title} → {user_id}")

    async def decide_refund(message: Message, command: CommandObject, decision: str) -> None:
        """/refunded <№> [notify] и /reject <№> [notify]. № — номер заявления из сводки."""
        if not is_owner(message):
            return
        name = command.command
        parts = (command.args or "").split()
        notify = len(parts) == 2 and parts[1].lower() == "notify"
        if not parts or not parts[0].lstrip("№#").isdigit() or (len(parts) == 2 and not notify) \
                or len(parts) > 2:
            await message.answer(f"Формат: /{name} <№ заявления> или /{name} <№> notify "
                                 "(№ — из сводки).")
            return
        req = support.resolve_refund(db, int(parts[0].lstrip("№#")), decision)
        if req is None:
            await message.answer("Открытого заявления с таким номером нет (уже решено или номер неверный).")
            return
        if decision == "refunded":
            note = (f"Заявление №{req['id']}: возврат отмечен выполненным. "
                    f"user_id {req['user_id']} больше не получает предложений.")
        else:
            note = f"Заявление №{req['id']}: отклонено. Пауза продаж снята, отметка «не продавать» не ставится."
        if notify:
            text = texts.REFUNDED_NOTIFY if decision == "refunded" else texts.REJECT_NOTIFY
            try:
                await message.bot.send_message(req["user_id"], text)
                note += " Клиенту отправлено сообщение о решении."
            except TelegramAPIError as e:
                note += f" Сообщение клиенту не доставлено: {e.message}."
        else:
            note += " Клиенту ничего не отправлено."
        await message.answer(note)

    @router.message(Command("refunded"))
    async def refunded(message: Message, command: CommandObject) -> None:
        await decide_refund(message, command, "refunded")

    @router.message(Command("reject"))
    async def reject(message: Message, command: CommandObject) -> None:
        await decide_refund(message, command, "rejected")

    @router.message(Command("summary"))
    async def summary_cmd(message: Message) -> None:
        if not is_owner(message):
            return
        await message.answer(summary.build(db, summary.msk(message.date)))

    @router.message(Command("stats"))
    async def stats(message: Message) -> None:
        if not is_owner(message):
            return
        await message.answer(f"Пользователей в базе: {db.count_users()}; "
                             f"подписаны на новости: {len(db.marketing_recipients())}; "
                             f"заявлений о возврате без решения: {len(db.refund_requests(open_only=True))}")

    # --- меню ------------------------------------------------------------------
    @router.message(F.text == texts.BTN_GUIDES)
    async def guides(message: Message, state: FSMContext) -> None:
        await state.clear()
        if not db.has_consent(message.from_user.id):
            await message.answer(texts.NEED_CONSENT)
            return
        if dctx.crisis_cooldown.active(message.from_user.id):
            # после кризисного сообщения не продаём, даже по кнопке
            await message.answer(safety.CRISIS_REPLY, reply_markup=main_menu())
            return
        if sell_blocked(message.from_user.id):
            # после выполненного возврата или пока заявление без решения: только описания, без цен, кнопок оплаты и призывов
            await message.answer(texts.GUIDES_INTRO_INFO, reply_markup=main_menu())
            for p in catalog.PRODUCTS.values():
                if not p.bundle:
                    await message.answer(f"{p.title}\n\n{p.description}")
            return
        await message.answer(texts.GUIDES_INTRO, reply_markup=main_menu())
        for p in catalog.PRODUCTS.values():
            link = cfg.pay_links.get(p.key, "")
            body = f"{p.title}\nЦена: {p.price_text}\n\n{p.description}"
            if not link:
                body += "\n\n" + texts.PAY_LINK_MISSING
            await message.answer(body, reply_markup=pay_kb(link, p.price_text))

    @router.message(F.text == texts.BTN_ASK)
    async def ask(message: Message, state: FSMContext) -> None:
        await state.clear()
        if not db.has_consent(message.from_user.id):
            await message.answer(texts.NEED_CONSENT)
            return
        dctx.introduced.add(message.from_user.id)
        await message.answer(texts.ASK_INTRO.format(intro=texts.victoria_intro()),
                             reply_markup=main_menu())

    @router.message(F.text.in_(LEGACY_BUTTONS))
    async def legacy_button(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer(texts.LEGACY_BUTTON, reply_markup=main_menu())

    # --- любой другой текст → Виктория ----------------------------------------
    @router.message(F.text)
    async def free_text(message: Message) -> None:
        if (message.text or "").startswith("/"):
            await message.answer(texts.HELP)
            return
        uid = message.from_user.id
        if not db.has_consent(uid):
            # Кризис проверяем даже без согласия: жизнь важнее воронки.
            if safety.detect_crisis(message.text or "").is_crisis:
                await apply_decision(message, await process_text(dctx, uid, message.text or ""))
                return
            await message.answer(texts.NEED_CONSENT)
            return
        await message.bot.send_chat_action(uid, "typing")
        await apply_decision(message, await process_text(dctx, uid, message.text or ""))

    @router.message()
    async def non_text(message: Message) -> None:
        await message.answer(texts.NON_TEXT)

    return router
