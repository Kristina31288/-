"""Обработчики Telegram (aiogram 3)."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
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
import safety
import texts
from config import Config
from dialog import DialogContext, Decision, Kind, process_text
from storage import Storage

log = logging.getLogger(__name__)


class Flow(StatesGroup):
    asking = State()          # вопросы ИИ-помощнику
    writing_author = State()  # следующее сообщение — автору


@dataclass
class Deps:
    config: Config
    storage: Storage
    dialog: DialogContext


# --- клавиатуры -------------------------------------------------------------
def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=texts.BTN_GUIDES), KeyboardButton(text=texts.BTN_CONSULT)],
            [KeyboardButton(text=texts.BTN_ASK), KeyboardButton(text=texts.BTN_AUTHOR)],
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


def build_router(deps: Deps) -> Router:
    router = Router(name="main")
    cfg, db, dctx = deps.config, deps.storage, deps.dialog

    def is_owner(m: Message) -> bool:
        return m.from_user is not None and m.from_user.id == cfg.owner_id

    async def apply_decision(message: Message, d: Decision) -> None:
        uid = message.from_user.id
        await message.answer(d.reply, reply_markup=main_menu())
        if cfg.log_dialogs and db.has_consent(uid):
            if d.kind is Kind.CRISIS:
                db.log_dialog(uid, "event", "сработал кризисный сценарий (текст не сохраняется)")
            elif d.log_user_text:
                db.log_dialog(uid, "user", message.text or "")
                db.log_dialog(uid, "bot", d.reply)
        if d.notify_owner:
            await notify_owner(
                message.bot, cfg,
                f"[{d.owner_note}] {user_label(message.from_user)}:\n{message.text}\n\n"
                f"Ответить: /reply {uid} текст",
            )

    # --- /start и согласие ---------------------------------------------------
    @router.message(CommandStart())
    async def start(message: Message, state: FSMContext) -> None:
        await state.clear()
        if db.has_consent(message.from_user.id):
            await message.answer(texts.MENU_HINT, reply_markup=main_menu())
            return
        await message.answer(
            texts.WELCOME.format(project=cfg.project_name, policy=cfg.policy_url),
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
        await message.answer(texts.DELETE_CONFIRM, reply_markup=delete_kb())

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
        await message.bot.send_message(user_id, texts.FEEDBACK_ASK)
        await message.answer(f"Выдано: {product.title} → {user_id}")

    @router.message(Command("reply"))
    async def reply(message: Message, command: CommandObject) -> None:
        if not is_owner(message):
            return
        parts = (command.args or "").split(maxsplit=1)
        if len(parts) != 2 or not parts[0].isdigit():
            await message.answer("Формат: /reply <user_id> <текст>")
            return
        try:
            await message.bot.send_message(int(parts[0]), "Ответ автора:\n\n" + parts[1])
        except TelegramAPIError as e:
            await message.answer(f"Не удалось отправить: {e.message}")
            return
        await message.answer("Отправлено.")

    @router.message(Command("stats"))
    async def stats(message: Message) -> None:
        if not is_owner(message):
            return
        await message.answer(f"Пользователей в базе: {db.count_users()}; "
                             f"подписаны на новости: {len(db.marketing_recipients())}")

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
        await message.answer(texts.GUIDES_INTRO, reply_markup=main_menu())
        for p in catalog.PRODUCTS.values():
            link = cfg.pay_links.get(p.key, "")
            body = f"{p.title}\nЦена: {p.price_text}\n\n{p.description}"
            if not link:
                body += "\n\n" + texts.PAY_LINK_MISSING
            await message.answer(body, reply_markup=pay_kb(link, p.price_text))

    @router.message(F.text == texts.BTN_CONSULT)
    async def consult(message: Message, state: FSMContext) -> None:
        await state.clear()
        if not db.has_consent(message.from_user.id):
            await message.answer(texts.NEED_CONSENT)
            return
        if dctx.crisis_cooldown.active(message.from_user.id):
            await message.answer(safety.CRISIS_REPLY, reply_markup=main_menu())
            return
        body = texts.CONSULT_TEXT.format(
            title=catalog.CONSULTATION_TITLE,
            price=catalog.format_price(catalog.CONSULTATION_PRICE),
            description=catalog.CONSULTATION_DESCRIPTION,
        )
        if cfg.consult_link:
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=texts.CONSULT_BUTTON, url=cfg.consult_link)]])
            await message.answer(body, reply_markup=kb)
        else:
            await message.answer(body + "\n\n" + texts.CONSULT_LINK_MISSING)

    @router.message(F.text == texts.BTN_ASK)
    async def ask(message: Message, state: FSMContext) -> None:
        if not db.has_consent(message.from_user.id):
            await message.answer(texts.NEED_CONSENT)
            return
        await state.set_state(Flow.asking)
        await message.answer(texts.ASK_INTRO.format(project=cfg.project_name), reply_markup=main_menu())

    @router.message(F.text == texts.BTN_AUTHOR)
    async def author(message: Message, state: FSMContext) -> None:
        if not db.has_consent(message.from_user.id):
            await message.answer(texts.NEED_CONSENT)
            return
        await state.set_state(Flow.writing_author)
        body = texts.AUTHOR_INTRO
        if cfg.author_contact:
            body += "\n" + texts.AUTHOR_CONTACT_LINE.format(contact=cfg.author_contact)
        await message.answer(body)

    # --- сообщение автору ---------------------------------------------------------
    @router.message(StateFilter(Flow.writing_author), F.text)
    async def to_author(message: Message, state: FSMContext) -> None:
        text = message.text or ""
        crisis = safety.detect_crisis(text)
        if crisis.is_crisis:
            # Кризис важнее: фиксированный ответ + уведомление (через общий конвейер).
            await state.clear()
            await apply_decision(message, await process_text(dctx, message.from_user.id, text))
            return
        if not dctx.limiter.allow(message.from_user.id):
            await message.answer(safety.RATE_LIMITED_REPLY)
            return
        await state.clear()
        await notify_owner(
            message.bot, cfg,
            f"[сообщение автору] {user_label(message.from_user)}:\n"
            f"{safety.truncate(text, cfg.max_message_len * 3)}\n\n"
            f"Ответить: /reply {message.from_user.id} текст",
        )
        await message.answer(texts.AUTHOR_SENT, reply_markup=main_menu())

    # --- любой другой текст → помощник ---------------------------------------
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
