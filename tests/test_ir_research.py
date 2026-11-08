import re
from datetime import date

from earnings_bot.ir_research import issuer_links, matches_release, quarter_headline


def test_inspector_requires_same_record_release_evidence() -> None:
    headline = "NVIDIA Announces Financial Results for Second Quarter Fiscal 2027"
    day = date(2026, 8, 26)
    assert matches_release({"releases": [{"Headline": headline}]}, day, {headline})
    assert matches_release(
        [{"title": headline, "publish_date": "2026-08-26T20:20:00Z"}], day, set()
    )
    assert not matches_release(
        [{"title": headline}, {"publish_date": "2026-08-26T20:20:00Z"}], day, set()
    )
    assert not matches_release(
        [{"title": "NVIDIA Sets Conference Call for Earnings", "publish_date": "2026-08-26"}],
        day,
        set(),
    )


def test_issuer_listing_keeps_same_origin_links() -> None:
    links = issuer_links(
        "https://ir.crowdstrike.com/press-releases",
        b'<a href="/news/release">Quarterly results</a>'
        b'<a href="https://elsewhere.example/news/release">Other results</a>',
    )
    assert links == [("https://ir.crowdstrike.com/news/release", "Quarterly results")]


def test_browser_headline_requires_the_scheduled_fiscal_period() -> None:
    pattern = re.compile(quarter_headline(2027, 3), re.I)
    assert pattern.fullmatch("CrowdStrike Reports Third Quarter Fiscal Year 2027 Financial Results")
    assert pattern.fullmatch("CrowdStrike Reports Q3 FY27 Results")
    assert not pattern.fullmatch("CrowdStrike Announces Date of Fiscal Third Quarter 2027 Results")
    assert not pattern.fullmatch("CrowdStrike Reports Second Quarter Fiscal Year 2027 Results")
    assert not pattern.fullmatch("CrowdStrike Reports Third Quarter Fiscal Year 2026 Results")
