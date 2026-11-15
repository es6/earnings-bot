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


def test_no_alert_channel_is_set_until_one_is_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCORD_TOKEN", "not-a-real-token")
    for name in ("DISCORD_CHANNEL_ID", "DISCORD_SERVER_CHANNEL_IDS", "WATCHDOG_WEBHOOK_URL"):
        monkeypatch.delenv(name, raising=False)

    settings = load_settings()

    assert settings.discord_channel_id is None
    assert settings.alert_channel_ids == []
    assert settings.watchdog_webhook_url is None


@pytest.mark.parametrize(
    ("own", "others", "expected"),
    [
        ("5", "6, 7", [5, 6, 7]),
        ("5", "7,5,6,7", [5, 7, 6]),  # one list: the primary first, no repeats
        (None, "9", [9]),  # a single id is not a comma list
        ("5", None, [5]),
    ],
    ids=["own-first", "deduplicated", "single-server", "only-own"],
)
def test_alert_channels_list_the_primary_first_without_repeats(
    monkeypatch: pytest.MonkeyPatch, own: str | None, others: str | None, expected: list[int]
) -> None:
    monkeypatch.setenv("DISCORD_TOKEN", "not-a-real-token")
    monkeypatch.delenv("DISCORD_CHANNEL_ID", raising=False)
    monkeypatch.delenv("DISCORD_SERVER_CHANNEL_IDS", raising=False)
    if own:
        monkeypatch.setenv("DISCORD_CHANNEL_ID", own)
    if others:
        monkeypatch.setenv("DISCORD_SERVER_CHANNEL_IDS", others)

    assert load_settings().alert_channel_ids == expected


def test_token_is_hidden_in_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCORD_TOKEN", "not-a-real-token")

    assert "not-a-real-token" not in repr(load_settings())


def test_missing_token_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DISCORD_TOKEN", raising=False)

    with pytest.raises(ValidationError):
        load_settings()


def test_sec_user_agent_needs_a_contact_email(monkeypatch: pytest.MonkeyPatch) -> None:
    # SEC answers 403 "Undeclared Automated Tool" to requests without one
    monkeypatch.setenv("DISCORD_TOKEN", "not-a-real-token")
    monkeypatch.setenv("SEC_USER_AGENT", "Jane Doe jane@example.com")
    assert load_settings().sec_user_agent == "Jane Doe jane@example.com"

    monkeypatch.setenv("SEC_USER_AGENT", "earnings-bot")
    with pytest.raises(ValidationError):
        load_settings()
