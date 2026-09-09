from __future__ import annotations

from app.config import Settings, parse_employee_mappings
from app.formatter import format_assignees


USER_1 = "11111111-1111-1111-1111-111111111111"
USER_2 = "22222222-2222-2222-2222-222222222222"
USER_3 = "33333333-3333-3333-3333-333333333333"


def test_multiple_assignees_known_first_then_unknown_names() -> None:
    result = format_assignees(
        [USER_2, USER_1, USER_3],
        {USER_1: "@subpolare", USER_3: "@someone"},
        {USER_1: "Первый", USER_2: "Иван Иванов", USER_3: "Третий"},
    )
    assert result == "@subpolare, @someone и Иван Иванов"


def test_only_unknown_assignees_use_yougile_names_without_conjunction() -> None:
    result = format_assignees(
        [USER_1, USER_2],
        {},
        {USER_1: "Иван Иванов", USER_2: "Пётр Петров"},
    )
    assert result == "Иван Иванов, Пётр Петров"


def test_only_telegram_assignees_are_comma_separated() -> None:
    result = format_assignees(
        [USER_1, USER_2],
        {USER_1: "@subpolare", USER_2: "@someone"},
        {},
    )
    assert result == "@subpolare, @someone"


def test_no_assignees_returns_none() -> None:
    assert format_assignees([], {}, {}) is None


def test_unknown_yougile_mapping_is_ignored() -> None:
    mappings = parse_employee_mappings({"YAN_YG": "unknown", "YAN_TG": "@valid_user"})
    assert mappings == {}


def test_empty_or_invalid_telegram_mapping_is_ignored() -> None:
    values = {
        "IVAN_YG": USER_1,
        "IVAN_TG": "@",
        "PETR_YG": USER_2,
        "PETR_TG": "",
    }
    assert parse_employee_mappings(values) == {}


def test_valid_mapping_is_discovered_generically() -> None:
    assert parse_employee_mappings({"ANY_PREFIX_YG": USER_1, "ANY_PREFIX_TG": "@valid_user"}) == {
        USER_1: "@valid_user"
    }


def test_settings_builds_asyncmy_url_without_real_environment() -> None:
    settings = Settings(
        _env_file=None,
        yougile_company_id="company",
        yougile_api_key="api-key",
        telegram_bot_token="123456:fake-token",
        mysql_host="db",
        mysql_password="p@ss word",
    )
    assert settings.sqlalchemy_url.startswith("mysql+asyncmy://")
    assert "p%40ss+word@db:3306/" in settings.sqlalchemy_url
