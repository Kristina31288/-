"""Сквозная проверка обработчиков aiogram без сети и без реального Telegram.

Запросы бота перехватываются фальшивой сессией. Если aiogram не установлен,
тест пропускается (остальные тесты от aiogram не зависят).
"""
import asyncio
import datetime as dt

import pytest

aiogram = pytest.importorskip("aiogram")

from aiogram import Bot, Dispatcher  # noqa: E402
from aiogram.client.session.base import BaseSession  # noqa: E402
from aiogram.fsm.storage.memory import MemoryStorage  # noqa: E402
from aiogram.methods import AnswerCallbackQuery, SendDocument, SendMessage  # noqa: E402
from aiogram.types import CallbackQuery, Chat, Message, Update, User  # noqa: E402

import safety  # noqa: E402
import texts  # noqa: E402
from config import load_config  # noqa: E402
from dialog import DialogContext  # noqa: E402
from faq import FaqBase  # noqa: E402
from handlers import Deps, build_router  # noqa: E402
from llm import FaqAssistant  # noqa: E402
from storage import Storage  # noqa: E402

OWNER = 1000
USER = 2000
TOKEN = "123456:TESTtokenTESTtokenTESTtokenTESTtok"


class FakeSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []
        self._mid = 0

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if isinstance(method, (SendMessage, SendDocument)):
            self._mid += 1
            return Message(message_id=self._mid, date=dt.datetime.now(),
                           chat=Chat(id=method.chat_id, type="private"),
                           text=getattr(method, "text", None))
        return True

    async def stream_content(self, *a, **kw):  # pragma: no cover
        raise NotImplementedError

    async def close(self):
        pass

    def sent(self, chat_id=None):
        return [c for c in self.calls if isinstance(c, (SendMessage, SendDocument))
                and (chat_id is None or c.chat_id == chat_id)]


def make_env(tmp_path, extra=None):
    files = tmp_path / "files"
    files.mkdir()
    (files / "00-lead-magnet.pdf").write_bytes(b"%PDF-1.4 lead")
    (files / "01-telefon.pdf").write_bytes(b"%PDF-1.4 t")
    cfg = load_config({
        "BOT_TOKEN": TOKEN, "OWNER_ID": str(OWNER),
        "PAY_LINK_TELEFON": "https://pay.example/1",
        "DB_PATH": str(tmp_path / "db.sqlite3"), "FILES_DIR": str(files),
        **(extra or {}),
    })
    db = Storage(cfg.db_path)
    dctx = DialogContext(
        assistant=FaqAssistant(FaqBase.load()), max_len=cfg.max_message_len,
        limiter=safety.RateLimiter(50, 60), crisis_cooldown=safety.Cooldown(3600),
        owner_notify_cooldown=safety.Cooldown(600), storage=db,
        notify_owner_on_crisis=cfg.notify_owner_on_crisis,
    )
    session = FakeSession()
    bot = Bot(TOKEN, session=session)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(build_router(Deps(cfg, db, dctx)))
    return bot, dp, session, db


@pytest.fixture
def env(tmp_path):
    bot, dp, session, db = make_env(tmp_path)
    yield bot, dp, session, db
    db.close()


@pytest.fixture
def env_notify(tmp_path):
    bot, dp, session, db = make_env(tmp_path, {"NOTIFY_OWNER_ON_CRISIS": "1"})
    yield bot, dp, session, db
    db.close()


_uid = 0


def msg(text, user_id=USER):
    global _uid
    _uid += 1
    u = User(id=user_id, is_bot=False, first_name="Тест")
    return Update(update_id=_uid, message=Message(
        message_id=_uid, date=dt.datetime.now(), chat=Chat(id=user_id, type="private"),
        from_user=u, text=text))


def cb(data, user_id=USER):
    global _uid
    _uid += 1
    u = User(id=user_id, is_bot=False, first_name="Тест")
    m = Message(message_id=1, date=dt.datetime.now(), chat=Chat(id=user_id, type="private"),
                from_user=u, text="x")
    return Update(update_id=_uid, callback_query=CallbackQuery(
        id=str(_uid), from_user=u, chat_instance="ci", message=m, data=data))


def feed(bot, dp, update):
    asyncio.run(dp.feed_update(bot, update))


def test_start_consent_lead_magnet_and_menu(env):
    bot, dp, s, db = env
    feed(bot, dp, msg("/start"))
    assert "[ССЫЛКА_ПОЛИТИКА]" in s.sent(USER)[-1].text
    feed(bot, dp, cb("consent:yes"))
    assert db.has_consent(USER)
    assert any(isinstance(c, SendDocument) for c in s.sent(USER))
    feed(bot, dp, msg(texts.BTN_GUIDES))
    assert any("299" in (getattr(c, "text", "") or "") for c in s.sent(USER))


def test_crisis_default_does_not_notify_owner(env):
    bot, dp, s, db = env
    feed(bot, dp, msg("/start"))
    feed(bot, dp, cb("consent:yes"))
    s.calls.clear()
    feed(bot, dp, msg("дочь говорит, что не хочет жить"))
    assert safety.CRISIS_REPLY in [c.text for c in s.sent(USER)]
    assert s.sent(OWNER) == []


def test_crisis_in_free_text_notifies_owner_and_does_not_sell(env_notify):
    bot, dp, s, db = env_notify
    feed(bot, dp, msg("/start"))
    feed(bot, dp, cb("consent:yes"))
    s.calls.clear()
    feed(bot, dp, msg("дочь говорит, что не хочет жить"))
    to_user = [c.text for c in s.sent(USER)]
    assert safety.CRISIS_REPLY in to_user
    assert any("кризис" in c.text for c in s.sent(OWNER))
    # текст кризиса не сохраняется в журнал
    assert all("не хочет жить" not in t for _, t in db.dialog(USER))
    # после кризиса по кнопке «Разборы» — снова помощь, а не цены
    s.calls.clear()
    feed(bot, dp, msg(texts.BTN_GUIDES))
    assert all("299" not in (c.text or "") for c in s.sent(USER))


def test_crisis_detected_even_without_consent(env):
    bot, dp, s, _ = env
    feed(bot, dp, msg("хочу умереть", user_id=3000))
    assert safety.CRISIS_REPLY in [c.text for c in s.sent(3000)]


def test_give_only_for_owner(env):
    bot, dp, s, db = env
    feed(bot, dp, msg("/start"))
    feed(bot, dp, cb("consent:yes"))
    s.calls.clear()
    feed(bot, dp, msg(f"/give {USER} telefon", user_id=USER))     # не владелица
    assert db.list_purchases(USER) == []
    feed(bot, dp, msg(f"/give {USER} telefon", user_id=OWNER))
    assert [p.product for p in db.list_purchases(USER)] == ["telefon"]
    assert any(isinstance(c, SendDocument) and c.chat_id == USER for c in s.calls)
    feed(bot, dp, msg(f"/give {USER} uroki", user_id=OWNER))       # файла нет
    assert "Нет файлов" in s.sent(OWNER)[-1].text
    assert [p.product for p in db.list_purchases(USER)] == ["telefon"]


def keyboard_texts(markup):
    return [b.text for row in markup.keyboard for b in row]


def all_texts(s, uid=USER):
    return [c.text or "" for c in s.sent(uid) if isinstance(c, SendMessage)]


def test_menu_has_no_consultation_or_author(env):
    bot, dp, s, _ = env
    feed(bot, dp, msg("/start"))
    assert "ИИ-помощница" in s.sent(USER)[-1].text and "Виктория" in s.sent(USER)[-1].text
    feed(bot, dp, cb("consent:yes"))
    menu = [c for c in s.sent(USER) if isinstance(c, SendMessage) and c.reply_markup is not None
            and hasattr(c.reply_markup, "keyboard")][-1].reply_markup
    assert keyboard_texts(menu) == [texts.BTN_GUIDES, texts.BTN_ASK]
    for t in keyboard_texts(menu):
        assert "Консультац" not in t and "автор" not in t.lower()
    s.calls.clear()
    feed(bot, dp, msg("Консультация"))                 # старая кнопка на клавиатуре
    assert all_texts(s) == [texts.LEGACY_BUTTON]
    feed(bot, dp, msg("/reply 1 привет", user_id=OWNER))   # команды «ответить от автора» нет
    assert not any("Ответ автора" in t for t in all_texts(s, 1))


def test_texts_have_no_consultation_or_write_to_author():
    import catalog
    for name in dir(texts):
        value = getattr(texts, name)
        if isinstance(value, str) and not name.startswith("__"):
            low = value.lower()
            for banned in ("консультац", "написать автору", "напишите автору", "живой человек",
                           "передаю автору", "вернём деньги"):
                assert banned not in low, (name, banned)
    assert not hasattr(texts, "BTN_CONSULT") and not hasattr(texts, "BTN_AUTHOR")
    assert not hasattr(catalog, "CONSULTATION_PRICE")


def test_refund_flow_no_owner_handoff_then_no_sell_everywhere(env):
    bot, dp, s, db = env
    feed(bot, dp, msg("/start"))
    feed(bot, dp, cb("consent:yes"))
    feed(bot, dp, cb("news:yes"))
    feed(bot, dp, msg(f"/give {USER} telefon", user_id=OWNER))
    pid = db.last_purchase_id(USER)
    s.calls.clear()
    feed(bot, dp, msg("верните деньги"))
    last = s.sent(USER)[-1]
    assert texts.REFUND_OFFER_SWAP in last.text
    assert last.reply_markup.inline_keyboard[0][0].callback_data.startswith(f"fu:swap:{pid}:")
    feed(bot, dp, msg("нет, верните деньги"))
    feed(bot, dp, msg("pay@mail.ru"))
    assert all_texts(s)[-1] == texts.REFUND_ACCEPTED
    assert s.sent(OWNER) == []                          # владелице ничего «вживую»
    assert db.is_no_sell(USER) and db.pending_for(USER) == []
    # «Разборы» — только описание, без кнопок оплаты
    s.calls.clear()
    feed(bot, dp, msg(texts.BTN_GUIDES))
    sent = [c for c in s.sent(USER) if isinstance(c, SendMessage)]
    assert sent and all("₽" not in (c.text or "") for c in sent)
    assert all(not getattr(c.reply_markup, "inline_keyboard", None) for c in sent)
    # карточка из цепочки и ответ «Как прошла неделя» — без оплаты
    s.calls.clear()
    feed(bot, dp, cb("fu:sit:uroki"))
    feed(bot, dp, cb(f"fu:wk:calm:{pid}"))
    for c in s.sent(USER):
        assert "₽" not in (getattr(c, "text", "") or "")
        kb = getattr(c.reply_markup, "inline_keyboard", None) or []
        assert all(b.url is None for row in kb for b in row)
    # старая кнопка замены не срабатывает
    feed(bot, dp, cb(f"fu:swap:{pid}:uroki"))
    assert all_texts(s)[-1] == texts.SWAP_AFTER_REFUND
    # Виктория не продаёт
    s.calls.clear()
    feed(bot, dp, msg("хочу новый разбор про уроки, сколько стоит?"))
    assert "₽" not in all_texts(s)[-1]
    # владелица: /refunded и сводка
    feed(bot, dp, msg("/summary", user_id=OWNER))
    rep = all_texts(s, OWNER)[-1]
    assert f"user_id {USER}" in rep and "pay@mail.ru" in rep and "Тест" not in rep
    feed(bot, dp, msg(f"/refunded {USER}", user_id=OWNER))
    assert "Отмечено" in all_texts(s, OWNER)[-1]
    assert db.refund_requests(open_only=True) == []


def test_file_missing_resends_purchase(env):
    bot, dp, s, db = env
    feed(bot, dp, msg("/start"))
    feed(bot, dp, cb("consent:yes"))
    feed(bot, dp, msg(f"/give {USER} telefon", user_id=OWNER))
    s.calls.clear()
    feed(bot, dp, msg("файл не пришёл"))
    assert any(isinstance(c, SendDocument) for c in s.sent(USER))


def test_gift_for_nabor_once(env, tmp_path):
    bot, dp, s, db = env
    feed(bot, dp, msg("/start"))
    feed(bot, dp, cb("consent:yes"))
    db.add_purchase(USER, "nabor")
    pid = db.last_purchase_id(USER)
    (tmp_path / "files" / "04-podarok.pdf").write_bytes(b"%PDF-1.4 gift")
    s.calls.clear()
    feed(bot, dp, cb(f"fu:gift:{pid}"))
    assert any(isinstance(c, SendDocument) for c in s.sent(USER))
    feed(bot, dp, cb(f"fu:gift:{pid}"))
    assert all_texts(s)[-1] == texts.GIFT_ALREADY


def test_delete_me(env):
    bot, dp, s, db = env
    feed(bot, dp, msg("/start"))
    feed(bot, dp, cb("consent:yes"))
    feed(bot, dp, msg("/delete_me"))
    feed(bot, dp, cb("delete:yes"))
    assert db.get_user(USER) is None
