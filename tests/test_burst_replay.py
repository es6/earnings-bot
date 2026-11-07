"""A few seconds of the burst replay, so its fixtures keep matching what the sources parse."""

from tests.burst_replay import Settings, replay


async def test_the_burst_replay_alerts_once_for_every_company_and_keeps_to_secs_limit() -> None:
    result = await replay(Settings(tickers=3, warmup=1.5, tail=2.0))

    assert result["missed_alerts"] == []
    assert result["duplicate_alerts"] == []
    assert result["no_body"] == []
    assert result["caught_up_at_first_poll"] == 0
    assert result["errors"] == 0
    # SEC's limit is 10 a second, and we keep to 8 in each one-second span of turns. Sends can
    # come out a few microseconds bunched, so this allows for one more.
    assert result["sec"]["most_in_any_second"] <= 9
    assert sum(result["alerts_from"].values()) == 3
