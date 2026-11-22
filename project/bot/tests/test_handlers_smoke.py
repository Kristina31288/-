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


@pytest.fixture
def env(tmp_path):
    files = tmp_path / "files"
    files.mkdir()
    (files / "00-lead-magnet.pdf").write_bytes(b"%PDF-1.4 lead")
    (files / "01-telefon.pdf").write_bytes(b"%PDF-1.4 t")
    cfg = load_config({
        "BOT_TOKEN": TOKEN, "OWNER_ID": str(OWNER),
        "PAY_LINK_TELEFON": "https://pay.example/1",
        "DB_PATH": str(tmp_path / "db.sqlite3"), "FILES_DIR": str(files),
    })
    db = Storage(cfg.db_path)
    dctx = DialogContext(
        assistant=FaqAssistant(FaqBase.load()), max_len=cfg.max_message_len,
        limiter=safety.RateLimiter(50, 60), crisis_cooldown=safety.Cooldown(3600),
        owner_notify_cooldown=safety.Cooldown(600),
    )
    session = FakeSession()
    bot = Bot(TOKEN, session=session)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(build_router(Deps(cfg, db, dctx)))
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


def test_crisis_in_free_text_notifies_owner_and_does_not_sell(env):
    bot, dp, s, db = env
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


def test_refund_is_handed_to_owner(env):
    bot, dp, s, _ = env
    feed(bot, dp, msg("/start"))
    feed(bot, dp, cb("consent:yes"))
    feed(bot, dp, msg("верните деньги"))
    assert any("передача (refund)" in c.text for c in s.sent(OWNER))


def test_delete_me(env):
    bot, dp, s, db = env
    feed(bot, dp, msg("/start"))
    feed(bot, dp, cb("consent:yes"))
    feed(bot, dp, msg("/delete_me"))
    feed(bot, dp, cb("delete:yes"))
    assert db.get_user(USER) is None
