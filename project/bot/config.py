"""Конфигурация из переменных окружения. Секреты в коде не хранятся.

Только стандартная библиотека. Файл .env читается простым парсером
(без python-dotenv); переменные, уже заданные в окружении, имеют приоритет.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

BASE_DIR = Path(__file__).resolve().parent

# Ключи продуктов, для которых есть ссылка на оплату PAY_LINK_<KEY>.
PAY_LINK_KEYS = ("TELEFON", "PODROSTOK", "UROKI", "NABOR")
# PDF-файлы продуктов: FILE_<KEY>, по умолчанию — имена ниже, лежат в FILES_DIR.
DEFAULT_PRODUCT_FILES = {
    "telefon": "01-telefon.pdf",
    "podrostok": "02-podrostok.pdf",
    "uroki": "03-uroki.pdf",
}
POLICY_PLACEHOLDER = "[ССЫЛКА_ПОЛИТИКА]"


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Config:
    bot_token: str
    owner_id: int
    pay_links: dict[str, str] = field(default_factory=dict)   # telefon/podrostok/uroki/nabor -> url
    consult_link: str = ""
    author_contact: str = ""
    policy_url: str = POLICY_PLACEHOLDER
    project_name: str = "Что сказать, когда…"
    gigachat_auth_key: str | None = None
    gigachat_scope: str = "GIGACHAT_API_PERS"
    gigachat_model: str = "GigaChat"
    gigachat_oauth_url: str | None = None
    gigachat_api_base: str | None = None
    gigachat_ca_bundle: str | None = None
    db_path: str = str(BASE_DIR / "data" / "bot.sqlite3")
    files_dir: str = str(BASE_DIR / "files")
    lead_magnet_file: str = "00-lead-magnet.pdf"
    product_files: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_PRODUCT_FILES))
    max_message_len: int = 1000
    rate_limit_count: int = 6
    rate_limit_window: int = 60
    dialog_log_days: int = 30
    log_dialogs: bool = True

    @property
    def llm_enabled(self) -> bool:
        return bool(self.gigachat_auth_key)


def parse_dotenv(text: str) -> dict[str, str]:
    """Минимальный парсер .env: KEY=VALUE, комментарии #, кавычки."""
    result: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        if key:
            result[key] = value
    return result


def load_dotenv_file(path: str | Path, environ: dict | None = None) -> None:
    """Подмешивает .env в окружение, не перезаписывая уже заданные переменные."""
    env = os.environ if environ is None else environ
    p = Path(path)
    if not p.is_file():
        return
    for k, v in parse_dotenv(p.read_text(encoding="utf-8")).items():
        env.setdefault(k, v)


def _int(env: Mapping[str, str], key: str, default: int, minimum: int = 0) -> int:
    raw = (env.get(key) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as e:
        raise ConfigError(f"{key} должен быть целым числом, сейчас: {raw!r}") from e
    if value < minimum:
        raise ConfigError(f"{key} должен быть не меньше {minimum}")
    return value


def _bool(env: Mapping[str, str], key: str, default: bool) -> bool:
    raw = (env.get(key) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on", "да")


def _link(env: Mapping[str, str], key: str) -> str:
    """Ссылка принимается только https:// (Telegram не примет кнопку с мусором)."""
    raw = (env.get(key) or "").strip()
    if not raw:
        return ""
    if not raw.startswith("https://"):
        raise ConfigError(f"{key} должна начинаться с https:// — сейчас: {raw!r}")
    return raw


def load_config(env: Mapping[str, str] | None = None) -> Config:
    env = os.environ if env is None else env

    token = (env.get("BOT_TOKEN") or "").strip()
    if not token:
        raise ConfigError("Не задан BOT_TOKEN (получите у @BotFather и пропишите в .env)")
    if ":" not in token:
        raise ConfigError("BOT_TOKEN выглядит неправильно: ожидается вид 123456:ABC...")

    owner_raw = (env.get("OWNER_ID") or "").strip()
    if not owner_raw:
        raise ConfigError("Не задан OWNER_ID (ваш числовой Telegram ID)")
    try:
        owner_id = int(owner_raw)
    except ValueError as e:
        raise ConfigError(f"OWNER_ID должен быть числом, сейчас: {owner_raw!r}") from e

    pay_links = {}
    for key in PAY_LINK_KEYS:
        link = _link(env, f"PAY_LINK_{key}")
        if link:
            pay_links[key.lower()] = link

    product_files = dict(DEFAULT_PRODUCT_FILES)
    for key in DEFAULT_PRODUCT_FILES:
        custom = (env.get(f"FILE_{key.upper()}") or "").strip()
        if custom:
            product_files[key] = custom

    db_path = (env.get("DB_PATH") or "").strip() or str(BASE_DIR / "data" / "bot.sqlite3")
    files_dir = (env.get("FILES_DIR") or "").strip() or str(BASE_DIR / "files")
    policy_url = (env.get("POLICY_URL") or "").strip() or POLICY_PLACEHOLDER

    auth_key = (env.get("GIGACHAT_AUTH_KEY") or "").strip() or None
    scope = (env.get("GIGACHAT_SCOPE") or "").strip() or "GIGACHAT_API_PERS"
    if scope not in ("GIGACHAT_API_PERS", "GIGACHAT_API_B2B", "GIGACHAT_API_CORP"):
        raise ConfigError(f"GIGACHAT_SCOPE: неизвестное значение {scope!r}")

    return Config(
        bot_token=token,
        owner_id=owner_id,
        pay_links=pay_links,
        consult_link=_link(env, "CONSULT_LINK"),
        author_contact=(env.get("AUTHOR_CONTACT") or "").strip(),
        policy_url=policy_url,
        project_name=(env.get("PROJECT_NAME") or "").strip() or "Что сказать, когда…",
        gigachat_auth_key=auth_key,
        gigachat_scope=scope,
        gigachat_model=(env.get("GIGACHAT_MODEL") or "").strip() or "GigaChat",
        gigachat_oauth_url=(env.get("GIGACHAT_OAUTH_URL") or "").strip() or None,
        gigachat_api_base=(env.get("GIGACHAT_API_BASE") or "").strip() or None,
        gigachat_ca_bundle=(env.get("GIGACHAT_CA_BUNDLE") or "").strip() or None,
        db_path=db_path,
        files_dir=files_dir,
        lead_magnet_file=(env.get("LEAD_MAGNET_FILE") or "").strip() or "00-lead-magnet.pdf",
        product_files=product_files,
        max_message_len=_int(env, "MAX_MESSAGE_LEN", 1000, minimum=50),
        rate_limit_count=_int(env, "RATE_LIMIT_COUNT", 6, minimum=1),
        rate_limit_window=_int(env, "RATE_LIMIT_WINDOW", 60, minimum=1),
        dialog_log_days=_int(env, "DIALOG_LOG_DAYS", 30, minimum=0),
        log_dialogs=_bool(env, "LOG_DIALOGS", True),
    )
