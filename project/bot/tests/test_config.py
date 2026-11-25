import pytest

from catalog import PRODUCTS, files_for, format_price, resolve_product
from config import ConfigError, load_config, load_dotenv_file, parse_dotenv

BASE = {"BOT_TOKEN": "123456:ABCdef", "OWNER_ID": "42"}


def test_minimal_config_defaults():
    c = load_config(dict(BASE))
    assert c.owner_id == 42
    assert c.pay_links == {}
    assert c.policy_url == "[ССЫЛКА_ПОЛИТИКА]"
    assert not c.llm_enabled
    assert c.gigachat_scope == "GIGACHAT_API_PERS"
    assert c.max_message_len == 1000
    assert c.notify_owner_on_crisis is False        # кризисные уведомления владелице выключены
    assert c.gift_file == "04-podarok.pdf"


@pytest.mark.parametrize("env", [
    {},
    {"BOT_TOKEN": "123456:ABC"},
    {"OWNER_ID": "1"},
    {"BOT_TOKEN": "bad", "OWNER_ID": "1"},
    {"BOT_TOKEN": "1:A", "OWNER_ID": "abc"},
    {**BASE, "PAY_LINK_TELEFON": "http://insecure.example"},
    {**BASE, "PAY_LINK_UPGRADE": "javascript:alert(1)"},
    {**BASE, "RATE_LIMIT_COUNT": "0"},
    {**BASE, "MAX_MESSAGE_LEN": "много"},
    {**BASE, "GIGACHAT_SCOPE": "WHATEVER"},
])
def test_invalid_config(env):
    with pytest.raises(ConfigError):
        load_config(env)


def test_full_config():
    env = {
        **BASE,
        "PAY_LINK_TELEFON": "https://pay.example/1",
        "PAY_LINK_NABOR": "https://pay.example/all",
        "NOTIFY_OWNER_ON_CRISIS": "1",
        "FILE_GIFT_EVENING": "gift.pdf",
        "GIGACHAT_AUTH_KEY": "c2VjcmV0",
        "DB_PATH": "/tmp/x.sqlite3",
        "FILE_UROKI": "uroki-v2.pdf",
        "LOG_DIALOGS": "0",
    }
    c = load_config(env)
    assert c.pay_links == {"telefon": "https://pay.example/1", "nabor": "https://pay.example/all"}
    assert c.notify_owner_on_crisis is True
    assert c.gift_file == "gift.pdf"
    assert not hasattr(c, "consult_link") and not hasattr(c, "author_contact")
    assert c.llm_enabled
    assert c.db_path == "/tmp/x.sqlite3"
    assert c.product_files["uroki"] == "uroki-v2.pdf"
    assert c.log_dialogs is False


def test_parse_dotenv():
    text = '# comment\nBOT_TOKEN="1:A b"\nexport OWNER_ID=7\nEMPTY=\nX=1 # tail\nbroken line\n'
    assert parse_dotenv(text) == {"BOT_TOKEN": "1:A b", "OWNER_ID": "7", "EMPTY": "", "X": "1"}


def test_dotenv_does_not_override_existing(tmp_path):
    p = tmp_path / ".env"
    p.write_text("OWNER_ID=1\nBOT_TOKEN=1:A\n", encoding="utf-8")
    env = {"OWNER_ID": "99"}
    load_dotenv_file(p, env)
    assert env == {"OWNER_ID": "99", "BOT_TOKEN": "1:A"}


def test_env_example_lists_required_keys():
    from pathlib import Path
    example = (Path(__file__).resolve().parent.parent / ".env.example").read_text(encoding="utf-8")
    for key in ("BOT_TOKEN", "OWNER_ID", "PAY_LINK_TELEFON", "PAY_LINK_PODROSTOK", "PAY_LINK_UROKI",
                "PAY_LINK_NABOR", "NOTIFY_OWNER_ON_CRISIS", "FILE_GIFT_EVENING",
                "GIGACHAT_AUTH_KEY", "DB_PATH"):
        assert f"{key}=" in example
    for removed in ("CONSULT_LINK", "AUTHOR_CONTACT", "онсультац"):
        assert removed not in example, removed
    # в примере не должно быть настоящих секретов
    for line in example.splitlines():
        if line.startswith(("BOT_TOKEN=", "GIGACHAT_AUTH_KEY=")):
            assert line.split("=", 1)[1].strip() == ""


def test_catalog_prices_from_brand():
    assert [PRODUCTS[k].price_rub for k in ("telefon", "podrostok", "uroki", "nabor")] == [299, 299, 299, 690]
    assert format_price(1500) == "1 500 ₽"
    assert resolve_product("2").key == "podrostok"
    assert resolve_product("Набор").key == "nabor"
    assert resolve_product("x") is None
    c = load_config(dict(BASE))
    assert [p.name for p in files_for(PRODUCTS["nabor"], c)] == ["01-telefon.pdf", "02-podrostok.pdf", "03-uroki.pdf"]
