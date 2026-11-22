"""ИИ-помощник: общий интерфейс, реализация GigaChat и режим без ИИ (FAQ).

Импорт этого модуля НЕ требует aiohttp: сетевая библиотека подгружается
только при реальном запросе к GigaChat. Поэтому тесты работают без сети.

Порядок вызова в боте (dialog.py): сначала safety (кризис → фиксированный
ответ, без ИИ), потом передача человеку, и только затем — этот модуль.
"""
from __future__ import annotations

import asyncio
import logging
import ssl
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Sequence

from faq import FaqBase
from safety import truncate

log = logging.getLogger(__name__)

KNOWLEDGE_PATH = Path(__file__).resolve().parent / "knowledge.md"
HANDOFF_MARKER = "[ПЕРЕДАТЬ_АВТОРУ]"
MAX_ANSWER_LEN = 3500   # лимит Telegram — 4096 символов

# ---------------------------------------------------------------------------
# GigaChat: эндпоинты. ПРОВЕРИТЬ ПО ДОКУМЕНТАЦИИ ПЕРЕД ЗАПУСКОМ:
# https://developers.sber.ru/docs/ru/gigachat/api/authorization
# https://developers.sber.ru/docs/ru/gigachat/api/reference/rest/gigachat-api
#
# Что удалось подтвердить поиском (ноябрь 2026):
#  - токен: POST https://ngw.devices.sberbank.ru:9443/api/v2/oauth,
#    заголовки Authorization: Basic <ключ авторизации>, RqUID: <uuid4>,
#    Content-Type: application/x-www-form-urlencoded, тело scope=GIGACHAT_API_PERS;
#    ответ: {"access_token": "...", "expires_at": <мс>}, токен живёт ~30 минут;
#  - запросы к модели: Authorization: Bearer <access_token>,
#    POST <API_BASE>/chat/completions.
# НЕ подтверждено: с 17.07.2026 Сбер называет целевым адресом https://api.giga.chat
# (в примерах SDK — base_url "https://api.giga.chat/v1"); прежний адрес
# gigachat.devices.sberbank.ru оставлен для подключившихся раньше. Точный путь
# и адрес OAuth для нового домена проверьте в личном кабинете/документации и
# при необходимости переопределите GIGACHAT_API_BASE / GIGACHAT_OAUTH_URL в .env.
# ---------------------------------------------------------------------------
GIGACHAT_OAUTH_URL = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"   # TODO: проверить
GIGACHAT_API_BASE = "https://gigachat.devices.sberbank.ru/api/v1"          # TODO: проверить (api.giga.chat?)
GIGACHAT_CHAT_PATH = "/chat/completions"                                   # TODO: проверить
TOKEN_REFRESH_MARGIN_S = 60


@dataclass(frozen=True)
class AssistantReply:
    text: str
    handoff: bool = False         # передать диалог владелице
    source: str = "faq"           # faq | gigachat


class Assistant(Protocol):
    async def answer(self, user_text: str, history: Sequence[tuple[str, str]] = (),
                     no_sales: bool = False) -> AssistantReply: ...


# ---------------------------------------------------------------------------
# Системный промпт: правила консультанта и продавца для этого проекта
# (тон и запреты — из brand.md, разделы 5 и 7).
# ---------------------------------------------------------------------------
SYSTEM_PROMPT_TEMPLATE = """Ты ИИ-помощник проекта «{project}» — библиотеки практических разборов трудных ситуаций с детьми. Ты консультант и помогаешь выбрать разбор.

КТО ТЫ
- Сразу и честно признавай, что ты ИИ. В первом ответе представься: «Я ИИ-помощник проекта «{project}»».
- Автор проекта — педагог. Никогда не называй автора детским психологом, психотерапевтом или клиническим психологом.

КАК ГОВОРИТЬ
- По-русски, на «вы», тепло и прямо, как опытная подруга-педагог. Без морализаторства и упрёков родителю.
- Без нытья и лишнего сочувствия: 2–5 предложений, сначала конкретный ответ, потом при необходимости одно предложение поддержки.
- Не начинай с «Мне так жаль», «Понимаю, как вам тяжело» и похожих фраз, не извиняйся многословно, не драматизируй. Глаголы действия, короткие фразы. Пример: «Возьмите Разбор 2 — 299 ₽. Начните с первой фразы из таблицы сегодня вечером.»
- Человек читает с телефона: вместо длинного текста — короткий список.
- Без канцелярита и без слов «триггер», «абьюз», «токсичный», «ресурс».
- Исключение — признаки беды (см. «Безопасность»): там говори спокойно и по-человечески, сочувствие уместно.

ФАКТЫ
- Отвечай ТОЛЬКО на основании базы знаний ниже. Цены, состав, условия возврата — только оттуда.
- Если ответа в базе нет, ответь: «Этого я не знаю, передам вопрос автору.» и добавь в конце метку {marker}.
- Не выдумывай скидки, акции, отзывы, статистику, исследования, дефицит, сроки, «осталось N мест».

ЧЕГО НЕЛЬЗЯ
- Не обещай результат («через 7 дней ребёнок будет…»). Можно: «чаще всего становится спокойнее».
- Не ставь диагнозов, не давай медицинских, психологических и юридических советов. Материалы информационно-просветительские.
- Не советуй наказаний, унижения, слежки за перепиской, лишения еды или сна.
- Не проси и не принимай платёжные данные. Оплата только по кнопке в меню «Разборы» / «Консультация».
- Не спрашивай имя, школу, диагнозы и другие данные о ребёнке. Достаточно возраста и сути ситуации.
- Не давай ссылок, которых нет в базе знаний.

ПРОДАЖА (мягко)
- Задай 1–2 вопроса: сколько лет ребёнку и что именно беспокоит.
- Предложи ОДИН подходящий разбор (или набор, если ситуаций несколько, или консультацию, если нужен личный разговор). Объясни, что внутри, назови цену и условие возврата (7 дней) — и только потом скажи, что оплатить можно по кнопке в меню «Разборы».
- Можно один раз мягко повторить предложение. После отказа вежливо остановись и предложи бесплатные «10 фраз, которые останавливают ссору с ребёнком».
- На сомнения («есть бесплатно в интернете», «а вдруг не подойдёт») отвечай честно: что внутри, чего там нет, условия возврата.

ПЕРЕДАЧА АВТОРУ
- Если человек просит живого человека или автора, жалуется, злится, просит вернуть деньги, пишет, что не получил файл, или вопрос вне базы знаний — ответь коротко, что передаёшь вопрос автору, и добавь в конце метку {marker}.

БЕЗОПАСНОСТЬ
- Если в сообщении есть признаки опасности (мысли о смерти, нежелание жить, самоповреждения, насилие, травмы, отказ от еды, вещества) — ничего не продавай. Скажи, что здесь нужен специалист, дай телефон доверия для детей, подростков и родителей 8-800-2000-122 (бесплатно, круглосуточно) и 112 при угрозе жизни, и добавь метку {marker}.
{no_sales}
БАЗА ЗНАНИЙ
{knowledge}
"""

NO_SALES_ADDON = (
    "\nСЕЙЧАС НЕ ПРОДАВАЙ: недавно в диалоге были признаки беды. Не предлагай покупки и ссылки, "
    "только поддержи и напомни про 8-800-2000-122 и 112.\n"
)


def load_knowledge(path: str | Path = KNOWLEDGE_PATH) -> str:
    return Path(path).read_text(encoding="utf-8")


def build_system_prompt(knowledge: str, project: str = "Что сказать, когда…",
                        no_sales: bool = False) -> str:
    return SYSTEM_PROMPT_TEMPLATE.format(
        project=project,
        marker=HANDOFF_MARKER,
        knowledge=knowledge.strip(),
        no_sales=NO_SALES_ADDON if no_sales else "",
    )


def parse_model_output(raw: str) -> AssistantReply:
    """Убирает служебную метку передачи и обрезает слишком длинный ответ."""
    text = (raw or "").strip()
    handoff = HANDOFF_MARKER in text
    text = text.replace(HANDOFF_MARKER, "").strip()
    if not text:
        text = "Этого я не знаю, передам вопрос автору."
        handoff = True
    return AssistantReply(truncate(text, MAX_ANSWER_LEN), handoff=handoff, source="gigachat")


# ---------------------------------------------------------------------------
# Режим без ИИ: ответы по ключевым словам
# ---------------------------------------------------------------------------
class FaqAssistant:
    def __init__(self, faq: FaqBase) -> None:
        self.faq = faq

    async def answer(self, user_text: str, history: Sequence[tuple[str, str]] = (),
                     no_sales: bool = False) -> AssistantReply:
        text, found = self.faq.answer(user_text)
        return AssistantReply(text, handoff=not found, source="faq")


# ---------------------------------------------------------------------------
# GigaChat
# ---------------------------------------------------------------------------
class GigaChatError(RuntimeError):
    pass


class GigaChatAssistant:
    """Запросы к GigaChat через aiohttp. При любой ошибке — ответ из FAQ."""

    def __init__(self, auth_key: str, knowledge: str, fallback: FaqAssistant, *,
                 scope: str = "GIGACHAT_API_PERS", model: str = "GigaChat",
                 project: str = "Что сказать, когда…",
                 oauth_url: str | None = None, api_base: str | None = None,
                 ca_bundle: str | None = None, timeout_s: float = 30.0) -> None:
        if not auth_key:
            raise ValueError("Нужен ключ авторизации GigaChat")
        self._auth_key = auth_key
        self._knowledge = knowledge
        self._fallback = fallback
        self._scope = scope
        self._model = model
        self._project = project
        self._oauth_url = oauth_url or GIGACHAT_OAUTH_URL
        self._chat_url = (api_base or GIGACHAT_API_BASE).rstrip("/") + GIGACHAT_CHAT_PATH
        self._ca_bundle = ca_bundle
        self._timeout_s = timeout_s
        self._token: str | None = None
        self._token_expires_at = 0.0      # unix-время, секунды
        self._lock = asyncio.Lock()

    def _ssl_context(self) -> ssl.SSLContext:
        # Сертификаты GigaChat выпущены НУЦ Минцифры («Russian Trusted Root CA»).
        # Скачайте корневой сертификат с gosuslugi.ru/crt и укажите путь в GIGACHAT_CA_BUNDLE.
        ctx = ssl.create_default_context()
        if self._ca_bundle:
            ctx.load_verify_locations(cafile=self._ca_bundle)
        return ctx

    async def _get_token(self, session) -> str:
        async with self._lock:
            if self._token and time.time() < self._token_expires_at - TOKEN_REFRESH_MARGIN_S:
                return self._token
            headers = {
                "Authorization": f"Basic {self._auth_key}",
                "RqUID": str(uuid.uuid4()),
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            }
            async with session.post(self._oauth_url, headers=headers,
                                    data={"scope": self._scope}) as resp:
                if resp.status != 200:
                    raise GigaChatError(f"OAuth: HTTP {resp.status}")
                data = await resp.json()
            token = data.get("access_token")
            if not token:
                raise GigaChatError("OAuth: нет access_token в ответе")
            expires_at = data.get("expires_at")
            # expires_at приходит в миллисекундах; на всякий случай понимаем и секунды
            if isinstance(expires_at, (int, float)):
                self._token_expires_at = expires_at / 1000 if expires_at > 10**11 else float(expires_at)
            else:
                self._token_expires_at = time.time() + 25 * 60
            self._token = token
            return token

    def build_messages(self, user_text: str, history: Sequence[tuple[str, str]],
                       no_sales: bool) -> list[dict]:
        messages = [{"role": "system",
                     "content": build_system_prompt(self._knowledge, self._project, no_sales)}]
        for role, text in list(history)[-6:]:
            messages.append({"role": "assistant" if role == "bot" else "user", "content": text})
        messages.append({"role": "user", "content": user_text})
        return messages

    async def _complete(self, messages: list[dict]) -> str:
        import aiohttp  # ленивый импорт: тесты не требуют aiohttp

        timeout = aiohttp.ClientTimeout(total=self._timeout_s)
        connector = aiohttp.TCPConnector(ssl=self._ssl_context())
        async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
            token = await self._get_token(session)
            payload = {
                "model": self._model,
                "messages": messages,
                "temperature": 0.3,
                "max_tokens": 600,
            }
            headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
            async with session.post(self._chat_url, json=payload, headers=headers) as resp:
                if resp.status == 401:
                    self._token = None   # токен протух — в следующий раз возьмём новый
                if resp.status != 200:
                    raise GigaChatError(f"chat/completions: HTTP {resp.status}")
                data = await resp.json()
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            raise GigaChatError("Неожиданный формат ответа GigaChat") from e

    async def answer(self, user_text: str, history: Sequence[tuple[str, str]] = (),
                     no_sales: bool = False) -> AssistantReply:
        try:
            raw = await self._complete(self.build_messages(user_text, history, no_sales))
            return parse_model_output(raw)
        except Exception as e:  # сеть, TLS, лимиты — отвечаем из FAQ
            log.warning("GigaChat недоступен (%s), отвечаю из FAQ", type(e).__name__)
            return await self._fallback.answer(user_text, history, no_sales)


def make_assistant(config, faq: FaqBase | None = None, knowledge: str | None = None) -> Assistant:
    """GigaChat, если задан GIGACHAT_AUTH_KEY, иначе — ответы из faq.json."""
    fallback = FaqAssistant(faq or FaqBase.load())
    if not config.gigachat_auth_key:
        return fallback
    return GigaChatAssistant(
        config.gigachat_auth_key,
        knowledge if knowledge is not None else load_knowledge(),
        fallback,
        scope=config.gigachat_scope,
        model=config.gigachat_model,
        project=config.project_name,
        oauth_url=config.gigachat_oauth_url,
        api_base=config.gigachat_api_base,
        ca_bundle=config.gigachat_ca_bundle,
    )
