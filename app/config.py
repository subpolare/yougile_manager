from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Mapping
from urllib.parse import quote_plus
from uuid import UUID

from dotenv import dotenv_values
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


TELEGRAM_USERNAME_RE = re.compile(r"^@[A-Za-z0-9_]{5,32}$")


def parse_employee_mappings(values: Mapping[str, object]) -> dict[str, str]:
    """Build YouGile user ID -> Telegram username without hardcoded employees."""
    normalized = {
        str(key).upper(): str(value).strip()
        for key, value in values.items()
        if value and str(key).upper().endswith(("_YG", "_TG"))
    }
    result: dict[str, str] = {}
    for key, yougile_value in normalized.items():
        if not key.endswith("_YG") or yougile_value.casefold() == "unknown":
            continue
        try:
            user_id = str(UUID(yougile_value))
        except (ValueError, AttributeError):
            continue
        username = normalized.get(f"{key[:-3]}_TG", "")
        if username and not username.startswith("@"):
            username = "@" + username
        if TELEGRAM_USERNAME_RE.fullmatch(username):
            result[user_id] = username
    return result


def load_employee_mappings(env_file: str | Path = ".env") -> dict[str, str]:
    # Reading is deliberately isolated here; callers must never log the returned source data.
    file_values = dotenv_values(env_file) if Path(env_file).is_file() else {}
    combined: dict[str, object] = {**file_values, **os.environ}
    return parse_employee_mappings(combined)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    yougile_company_id: str
    yougile_api_key: SecretStr
    telegram_bot_token: SecretStr
    openai_api_key: SecretStr | None = None
    openai_transcription_model: str = "gpt-transcribe"
    openai_reminder_model: str = "gpt-5.6-luna"
    openai_error_model: str = "gpt-5.6-terra"
    error_admin_telegram_user_id: int | None = None
    sasha_tg: str | None = None
    yougile_base_url: str = "https://yougile.com/api-v2"

    database_url: SecretStr | None = None
    mysql_host: str = "mysql"
    mysql_port: int = 3306
    mysql_database: str = "yougile_bot"
    mysql_user: str = "yougile"
    mysql_password: SecretStr = SecretStr("yougile_dev")
    db_connect_attempts: int = 30
    db_connect_retry_seconds: float = 2.0

    log_level: str = "INFO"

    @property
    def sqlalchemy_url(self) -> str:
        if self.database_url is not None:
            return self.database_url.get_secret_value()
        user = quote_plus(self.mysql_user)
        password = quote_plus(self.mysql_password.get_secret_value())
        database = quote_plus(self.mysql_database)
        return (
            f"mysql+asyncmy://{user}:{password}@{self.mysql_host}:"
            f"{self.mysql_port}/{database}?charset=utf8mb4"
        )
