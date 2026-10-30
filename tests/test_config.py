import pytest
from pydantic import ValidationError

from earnings_bot.config import Settings


def load_settings() -> Settings:
    # _env_file=None ignores a developer's real .env file. Pyright can't see that
    # BaseSettings reads its fields from the environment, hence the ignore.
    return Settings(_env_file=None)  # pyright: ignore[reportCallIssue]


def test_reads_settings_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCORD_TOKEN", "not-a-real-token")
    monkeypatch.setenv("DISCORD_GUILD_ID", "123456789012345678")

    settings = load_settings()

    assert settings.discord_token.get_secret_value() == "not-a-real-token"
    assert settings.discord_guild_id == 123456789012345678
    assert settings.log_level == "INFO"


def test_empty_guild_id_means_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCORD_TOKEN", "not-a-real-token")
    monkeypatch.setenv("DISCORD_GUILD_ID", "")

    assert load_settings().discord_guild_id is None


def test_token_is_hidden_in_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCORD_TOKEN", "not-a-real-token")

    assert "not-a-real-token" not in repr(load_settings())


def test_missing_token_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DISCORD_TOKEN", raising=False)

    with pytest.raises(ValidationError):
        load_settings()
