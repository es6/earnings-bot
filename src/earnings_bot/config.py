"""Settings loaded from environment variables, or from a local .env file."""

from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

_ENV = SettingsConfigDict(
    env_file=".env",
    env_file_encoding="utf-8",
    env_ignore_empty=True,  # treat `DISCORD_GUILD_ID=` in .env as unset
    extra="ignore",
)

# SEC rejects requests unless the User-Agent names a person and a contact email,
# e.g. "Jane Doe jane@example.com"
_SEC_USER_AGENT = r"\S+@\S+\.\S+"


class Settings(BaseSettings):
    model_config = _ENV

    # SecretStr keeps the token out of logs and reprs; .get_secret_value() reads it.
    discord_token: SecretStr
    # Test server ID. When set, slash commands sync there instantly instead of globally.
    discord_guild_id: int | None = None
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    # The rest is for watching a schedule (`uv run earnings-bot schedules/<day>.toml`)
    discord_channel_id: int | None = None  # where releases get posted
    discord_status_channel_id: int | None = None  # optional watcher updates
    sec_user_agent: str | None = Field(None, pattern=_SEC_USER_AGENT)
    # Summaries by Muse Ember through Meta's Model API. They cost money per release, so they
    # run only when switched on: LIVE_SUMMARIES=true and META_MODEL_API_KEY
    live_summaries: bool = False
    meta_model_api_key: SecretStr | None = None
    fmp_api_key: SecretStr | None = None  # earnings-calendar lookups


class RecorderSettings(BaseSettings):
    model_config = _ENV

    sec_user_agent: str = Field(pattern=_SEC_USER_AGENT)
