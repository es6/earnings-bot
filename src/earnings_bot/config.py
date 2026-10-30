"""Settings loaded from environment variables, or from a local .env file."""

from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,  # treat `DISCORD_GUILD_ID=` in .env as unset
        extra="ignore",
    )

    # SecretStr keeps the token out of logs and reprs; .get_secret_value() reads it.
    discord_token: SecretStr
    # Test server ID. When set, slash commands sync there instantly instead of globally.
    discord_guild_id: int | None = None
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
