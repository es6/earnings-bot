from datetime import datetime

from earnings_bot.date_discovery import parse_notice
from earnings_bot.known import known_companies
from earnings_bot.recorder import ET

NOW = datetime(2026, 11, 7, 10, tzinfo=ET)


def test_notice_prefers_report_time_over_call_and_preserves_fiscal_year() -> None:
    result = parse_notice(
        "NVDA",
        known_companies()["NVDA"],
        "NVIDIA Sets Conference Call for Third-Quarter Financial Results",
        "<p>On Tuesday, November 17, 2026, at 2 p.m. PT, NVIDIA will host a conference call "
        "for the third quarter of fiscal year 2027.</p><p>This material will be posted to "
        "investor.nvidia.com immediately after the company's results are publicly announced "
        "at approximately 1:20 p.m. PT.</p>",
        NOW,
    )
    assert result is not None
    target, evidence = result
    assert target.expected == datetime(2026, 11, 17, 16, 20, tzinfo=ET)
    assert target.period == "FY2027Q3"
    assert evidence == "Issuer confirmed release time"


def test_notice_arms_wider_window_after_close_and_leaves_call_only_pending() -> None:
    company = known_companies()["CRWD"]
    title = "CrowdStrike Announces Date of Fiscal Third Quarter 2027 Financial Results"
    result = parse_notice(
        "CRWD",
        company,
        title,
        "CrowdStrike will release its financial results on December 1, 2026 after market close. "
        "The conference call will start at 5:00 p.m. ET.",
        NOW,
    )
    assert result is not None
    target, evidence = result
    assert target.expected.date().isoformat() == "2026-12-01"
    assert target.expected.hour == 16
    assert target.before_minutes == 60
    assert "after market close" in evidence
    assert (
        parse_notice(
            "CRWD",
            company,
            title,
            "CrowdStrike will host a conference call on December 1, 2026 at 5 p.m. ET.",
            NOW,
        )
        is None
    )
