"""Settings loaded from environment variables, or from a local .env file."""

from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

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
    # Alert channels in other servers, comma-separated. Each server tracks its own companies;
    # a company several servers track is watched and summarized once and posted to each.
    discord_server_channel_ids: Annotated[list[int], NoDecode] = []
    discord_status_channel_id: int | None = None  # optional watcher updates
    sec_user_agent: str | None = Field(None, pattern=_SEC_USER_AGENT)
    # Summaries by Muse Ember through Meta's Model API. They cost money per release, so they
    # run only when switched on: LIVE_SUMMARIES=true and META_MODEL_API_KEY
    live_summaries: bool = False
    # Separately opt in to paid /track driver profiles after reviewing their evaluation.
    live_driver_profiles: bool = False
    # Separately opt in to the experimental source-reference writer; never automatic.
    source_referenced_summaries: bool = False
    meta_model_api_key: SecretStr | None = None
    fmp_api_key: SecretStr | None = None  # earnings-calendar lookups

    @field_validator("discord_server_channel_ids", mode="before")
    @classmethod
    def _comma_separated(cls, value: object) -> object:
        if isinstance(value, str):
            return [int(part) for part in value.replace(" ", "").split(",") if part]
        if isinstance(value, int):
            return [value]
        return value

    @property
    def alert_channel_ids(self) -> list[int]:
        """Every server's alert channel, this deployment's own first."""
        first = [self.discord_channel_id] if self.discord_channel_id is not None else []
        return list(dict.fromkeys([*first, *self.discord_server_channel_ids]))

    # Where the watchdog (python -m earnings_bot.watchdog) posts its alarms
    watchdog_webhook_url: SecretStr | None = None


class RecorderSettings(BaseSettings):
    model_config = _ENV

    sec_user_agent: str = Field(pattern=_SEC_USER_AGENT)
