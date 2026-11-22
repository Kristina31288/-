"""Цепочка и «Как прошла неделя?» на фальшивом Telegram (без сети). Нужен aiogram."""
import asyncio
import datetime as dt

import pytest

pytest.importorskip("aiogram")

from aiogram import Bot, Dispatcher  # noqa: E402
from aiogram.exceptions import TelegramForbiddenError  # noqa: E402
from aiogram.fsm.storage.memory import MemoryStorage  # noqa: E402
from aiogram.methods import SendDocument, SendMessage  # noqa: E402
from aiogram.types import Chat, Message  # noqa: E402

import followup as fu  # noqa: E402
import safety  # noqa: E402
import texts  # noqa: E402
from config import load_config  # noqa: E402
from dialog import DialogContext  # noqa: E402
from faq import FaqBase  # noqa: E402
from handlers import Deps, build_router  # noqa: E402
from llm import FaqAssistant  # noqa: E402
from scheduler import send_due  # noqa: E402
from storage import Storage  # noqa: E402
from test_handlers_smoke import OWNER, TOKEN, USER, FakeSession, cb, feed, msg  # noqa: E402


class BlockingSession(FakeSession):
    """Как FakeSession, но для chat_id из blocked отвечает «бот заблокирован»."""

    def __init__(self):
        super().__init__()
        self.blocked = set()

    async def make_request(self, bot, method, timeout=None):
        if getattr(method, "chat_id", None) in self.blocked:
            raise TelegramForbiddenError(method=method, message="Forbidden: bot was blocked by the user")
        return await super().make_request(bot, method, timeout)


@pytest.fixture
def env(tmp_path):
    files = tmp_path / "files"
    files.mkdir()
    for name in ("00-lead-magnet.pdf", "01-telefon.pdf", "02-podrostok.pdf"):
        (files / name).write_bytes(b"%PDF-1.4")
    cfg = load_config({
        "BOT_TOKEN": TOKEN, "OWNER_ID": str(OWNER),
        "PAY_LINK_TELEFON": "https://pay.example/1", "PAY_LINK_PODROSTOK": "https://pay.example/2",
        "PAY_LINK_UROKI": "https://pay.example/3", "PAY_LINK_NABOR": "https://pay.example/all",
        "DB_PATH": str(tmp_path / "db.sqlite3"), "FILES_DIR": str(files),
    })
    db = Storage(cfg.db_path)
    dctx = DialogContext(
        assistant=FaqAssistant(FaqBase.load()), max_len=cfg.max_message_len,
        limiter=safety.RateLimiter(50, 60), crisis_cooldown=safety.Cooldown(3600),
        owner_notify_cooldown=safety.Cooldown(600),
    )
    session = BlockingSession()
    bot = Bot(TOKEN, session=session)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(build_router(Deps(cfg, db, dctx)))
    yield bot, dp, session, db, cfg
    db.close()


def subscribe(bot, dp):
    feed(bot, dp, msg("/start"))
    feed(bot, dp, cb("consent:yes"))
    feed(bot, dp, cb("news:yes"))


def texts_to(s, uid=USER):
    return [c.text for c in s.sent(uid) if isinstance(c, SendMessage)]


def test_consent_schedules_chain(env):
    bot, dp, s, db, _ = env
    subscribe(bot, dp)
    assert sorted(m.kind for m in db.pending_for(USER)) == ["tip1", "tip2", "tip3"]


def test_situation_button_shows_card_then_nabor(env):
    bot, dp, s, db, _ = env
    subscribe(bot, dp)
    s.calls.clear()
    feed(bot, dp, cb("fu:sit:podrostok"))
    sent = [c for c in s.sent(USER) if isinstance(c, SendMessage)]
    assert "Разбор 2" in sent[0].text and "299" in sent[0].text
    assert sent[0].reply_markup.inline_keyboard[0][0].url == "https://pay.example/2"
    assert "690" in sent[1].text
    assert sent[1].reply_markup.inline_keyboard[0][0].url == "https://pay.example/all"
    assert db.followup_state(USER).chosen_product == "podrostok"


def test_stop_tips_command_and_button(env):
    bot, dp, s, db, _ = env
    subscribe(bot, dp)
    feed(bot, dp, msg("/stop_tips"))
    assert db.pending_for(USER) == [] and db.followup_state(USER).tips_off
    assert texts.TIPS_STOPPED in texts_to(s)
    feed(bot, dp, cb("fu:stop"))                    # повторно — без ошибок
    assert texts_to(s)[-1] == texts.TIPS_STOPPED


def test_crisis_cancels_chain_and_blocks_card(env):
    bot, dp, s, db, _ = env
    subscribe(bot, dp)
    feed(bot, dp, msg("сын говорит, что не хочет жить"))
    assert db.pending_for(USER) == []
    assert db.followup_state(USER).crisis_at is not None
    s.calls.clear()
    feed(bot, dp, cb("fu:sit:telefon"))
    assert texts_to(s) == [safety.CRISIS_REPLY]


def test_delete_me_clears_queue(env):
    bot, dp, s, db, _ = env
    subscribe(bot, dp)
    feed(bot, dp, msg("/delete_me"))
    feed(bot, dp, cb("delete:yes"))
    assert db.scheduled_for(USER) == []


def test_scheduler_sends_in_window_and_marks_blocked(env):
    bot, dp, s, db, cfg = env
    subscribe(bot, dp)
    tip1 = [m for m in db.pending_for(USER) if m.kind == "tip1"][0]
    s.calls.clear()
    now = tip1.due_at + dt.timedelta(minutes=1)
    stats = asyncio.run(send_due(bot, db, cfg, now))
    assert stats["sent"] == 1
    sent = s.sent(USER)[-1]
    assert sent.text == texts.TIP1
    assert sent.reply_markup.inline_keyboard[-1][0].callback_data == "fu:stop"
    # пользователь заблокировал бота
    s.blocked.add(USER)
    tip2 = [m for m in db.pending_for(USER) if m.kind == "tip2"][0]
    stats = asyncio.run(send_due(bot, db, cfg, tip2.due_at + dt.timedelta(minutes=1)))
    assert stats["blocked"] == 1
    assert db.followup_state(USER).blocked and db.pending_for(USER) == []
    # вернулся через /start — отметка снимается
    s.blocked.clear()
    feed(bot, dp, msg("/start"))
    assert not db.followup_state(USER).blocked


def test_give_schedules_week_and_nofit_swaps_once(env):
    bot, dp, s, db, cfg = env
    subscribe(bot, dp)
    feed(bot, dp, msg(f"/give {USER} telefon", user_id=OWNER))
    assert [m.kind for m in db.pending_for(USER)] == ["week"]      # цепочке больше не продаём
    week = db.pending_for(USER)[0]
    pid = db.last_purchase_id(USER)
    assert week.ref == pid
    when = fu.next_daytime(week.due_at)
    asyncio.run(send_due(bot, db, cfg, when + dt.timedelta(minutes=1)))
    assert texts_to(s)[-1] == texts.WEEK
    s.calls.clear()
    feed(bot, dp, cb(f"fu:wk:nofit:{pid}"))
    assert texts_to(s)[-1] == texts.WEEK_NOFIT_SWAP
    s.calls.clear()
    feed(bot, dp, cb(f"fu:swap:{pid}:podrostok"))
    assert any(isinstance(c, SendDocument) for c in s.sent(USER))
    assert [p.source for p in db.list_purchases(USER)] == ["manual", "swap"]
    feed(bot, dp, cb(f"fu:swap:{pid}:uroki"))
    assert texts_to(s)[-1] == texts.SWAP_ALREADY


def test_swap_without_file_is_not_recorded(env):
    bot, dp, s, db, cfg = env
    subscribe(bot, dp)
    feed(bot, dp, msg(f"/give {USER} telefon", user_id=OWNER))
    pid = db.last_purchase_id(USER)
    feed(bot, dp, cb(f"fu:swap:{pid}:uroki"))                      # 03-uroki.pdf нет
    assert texts_to(s)[-1] == texts.SWAP_FILE_LATER
    assert [p.source for p in db.list_purchases(USER)] == ["manual"]
    assert any("Замена разбора" in c.text for c in s.sent(OWNER))


def test_week_hard_resends_lead_magnet(env):
    bot, dp, s, db, cfg = env
    subscribe(bot, dp)
    feed(bot, dp, msg(f"/give {USER} telefon", user_id=OWNER))
    s.calls.clear()
    feed(bot, dp, cb(f"fu:wk:hard:{db.last_purchase_id(USER)}"))
    assert isinstance(s.sent(USER)[0], SendDocument)
    joined = " ".join(texts_to(s))
    assert "[ССЫЛКА_ДОПЛАТА]" in joined and "8-800-2000-122" in joined
    assert "Консультац" not in joined
