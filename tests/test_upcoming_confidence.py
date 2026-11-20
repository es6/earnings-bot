from first_print.calendar import calendar_line
from tests.builders import uber


def test_confirmed_date_does_not_turn_a_historical_time_into_a_confirmed_time() -> None:
    target = uber()
    issuer = calendar_line(target, "Issuer confirmed release date", "🟠")
    assert "date confirmed · time estimated" in issuer
    exact = calendar_line(target, "Issuer confirmed release time", "🟢")
    assert "date and time confirmed" in exact
    for evidence in ("Schedule file", "Tracked", "FMP calendar date, time from past 8-Ks"):
        line = calendar_line(target, evidence, "🟡")
        assert "date confirmed" not in line and "time estimated" in line
