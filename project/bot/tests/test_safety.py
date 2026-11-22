"""Тесты безопасности. Только стандартная библиотека, без aiogram и сети."""
import pytest

import safety
from safety import Cooldown, RateLimiter, detect_crisis, detect_intent

CRISIS_POSITIVE = [
    # суицидальные высказывания, разные формы
    ("Я больше не хочу жить", "suicide"),
    ("не хочу жыть", "suicide"),
    ("нехочу жить", "suicide"),
    ("Сын сказал, что не хочет жить", "suicide"),
    ("дочь говорит что больше не хочет жить", "suicide"),
    ("жить не хочется совсем", "suicide"),
    ("НЕ ХОЧУ ЖИИИИТЬ!!!", "suicide"),
    ("Дочь пишет в дневнике про суицид", "suicide"),
    ("подросток думает о суециде", "suicide"),
    ("cуицид", "suicide"),                       # латинская c
    ("с у и ц и д", "suicide"),                  # по буквам
    ("сын читает про самоубиство", "suicide"),
    ("хочу покончить с собой", "suicide"),
    ("сказал что убьёт себя", "suicide"),
    ("Лучше бы меня не было", "suicide"),
    ("хочу умереть", "suicide"),
    ("хочу умиреть", "suicide"),
    ("незачем жить", "suicide"),
    ("говорит что хочет выпрыгнуть из окна", "suicide"),
    ("она наглоталась таблеток", "suicide"),
    ("сын говорит о смерти постоянно", "suicide"),
    ("грозится повеситься", "suicide"),
    ("I want to die", "suicide"),
    # самоповреждение
    ("сын режет себе руки", "self_harm"),
    ("увидела порезы на руках у дочери", "self_harm"),
    ("у подростка самоповреждения", "self_harm"),
    ("подозреваю селфхарм", "self_harm"),
    ("дочь наносит себе порезы", "self_harm"),
    # насилие
    ("муж бьёт ребёнка", "violence"),
    ("отчим избивает сына", "violence"),
    ("к дочке пристаёт отчим, пристаёт к ней", "violence"),
    ("сын угрожает убить сестру", "violence"),
    ("было сексуальное насилие", "violence"),
    ("брат ударил его так, что остались синяки", "violence"),
]

CRISIS_NEGATIVE = [
    "Ребёнок не хочет учиться, каждый вечер скандал",
    "Сын не отлипает от телефона",
    "Сколько стоит набор?",
    "Убил весь вечер на домашку",
    "Играет в игру, где надо убивать зомби",
    "Хочу вернуть деньги",
    "У малыша режутся зубы",
    "Как воспитывать без насилия?",
    "Я чуть не умерла со смеху",
    "Хотим повесить картину в детской",
    "Пошли повеселиться в парк",
    "Дочь хочет жить отдельно, ей 16",
    "Подросток жить не может без телефона",
    "Подросток хамит и закрылся",
    "как не кричать на ребёнка из-за уроков",
    "Резко пропала мотивация учиться",
    # Удар без травм — частая жалоба целевой аудитории («агрессивно реагирует,
    # когда забираю гаджет»), это зона консультанта, а не кризис.
    "Сын ударил меня, когда я забрала телефон",
    "",
]


@pytest.mark.parametrize("text,category", CRISIS_POSITIVE)
def test_crisis_detected(text, category):
    r = detect_crisis(text)
    assert r.is_crisis, text
    assert r.category == category, (text, r)


@pytest.mark.parametrize("text", CRISIS_NEGATIVE)
def test_crisis_not_detected(text):
    assert not detect_crisis(text).is_crisis, (text, detect_crisis(text))


def test_crisis_reply_has_helplines_and_no_sales():
    t = safety.CRISIS_REPLY
    assert "8-800-2000-122" in t
    assert "112" in t
    for word in ("₽", "оплат", "купит", "разбор", "http"):
        assert word not in t.lower()


# Намерения клиента: владелице «вживую» ничего не передаётся — на всё отвечает Виктория (support.py).
INTENT_POSITIVE = [
    ("Хочу поговорить с живым человеком", "human"),
    ("позовите автора", "human"),
    ("соедините с оператором", "human"),
    ("можно не с ботом?", "human"),
    ("Хочу пожаловаться", "complaint"),
    ("у меня жалоба", "complaint"),
    ("это обман", "complaint"),
    ("вы мошенники", "complaint"),
    ("Верните деньги!", "refund"),
    ("хочу вернуть деньги", "refund"),
    ("как оформить возврат", "refund"),
    ("бл*, верните деньги", "refund"),           # возврат важнее мата
    ("оплатила, но файл не пришёл", "file_missing"),
    ("не пришел файл", "file_missing"),
    ("файл не открывается", "file_missing"),
    ("не могу открыть файл", "file_missing"),
    ("разбор не подошёл", "nofit"),
    ("хочу другой разбор", "nofit"),
    ("можно замену?", "nofit"),
    ("да пошли вы на х*й", "profanity"),
    ("бля", "profanity"),
]

INTENT_NEGATIVE = [
    "Сколько стоит разбор?",
    "Какой разбор выбрать для 9 лет?",
    "Подросток потерял ко мне авторитет",
    "Сын не отлипает от телефона",
    "В семье 4 человека",
    "Кто автор разборов?",
    "Подросток не открывается мне",
    "Хочу заменить телефон на книги",
]


@pytest.mark.parametrize("text,intent", INTENT_POSITIVE)
def test_intent_detected(text, intent):
    r = detect_intent(text)
    assert r.found, text
    assert r.intent == intent, (text, r)


@pytest.mark.parametrize("text", INTENT_NEGATIVE)
def test_intent_not_detected(text):
    assert not detect_intent(text).found, (text, detect_intent(text))


def test_crisis_reply_unchanged():
    """Кризисный текст фиксирован решением владелицы — не меняется."""
    assert safety.CRISIS_REPLY.startswith("Мне жаль, что вам сейчас так тяжело.")
    assert "не оставайтесь с этим одни" in safety.CRISIS_REPLY.lower()


def test_length_helpers():
    assert safety.is_too_long("а" * 11, 10)
    assert not safety.is_too_long("а" * 10, 10)
    assert safety.truncate("абвгдеж", 5) == "абвг…"
    assert safety.truncate("абв", 5) == "абв"


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_rate_limiter_window():
    clock = FakeClock()
    rl = RateLimiter(3, 60, clock=clock)
    assert all(rl.allow(1) for _ in range(3))
    assert not rl.allow(1)
    assert rl.allow(2)            # другой пользователь не затронут
    clock.t += 61
    assert rl.allow(1)


def test_rate_limiter_forget_and_validation():
    rl = RateLimiter(1, 10, clock=FakeClock())
    assert rl.allow(5) and not rl.allow(5)
    rl.forget(5)
    assert rl.allow(5)
    with pytest.raises(ValueError):
        RateLimiter(0, 10)


def test_cooldown():
    clock = FakeClock()
    cd = Cooldown(100, clock=clock)
    assert not cd.active(1)
    cd.mark(1)
    assert cd.active(1)
    clock.t += 101
    assert not cd.active(1)
