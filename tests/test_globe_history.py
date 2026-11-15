from datetime import datetime

from earnings_bot.globe_history import listed, past_releases
from earnings_bot.recorder import ET
from tests.fakes import RoutedFetcher

WERIDE = "/en/search/organization/WeRide%2520Inc&#167;"


def row(when: str, organization: str, link: str, title: str, page: str = WERIDE) -> str:
    """One release as GlobeNewswire's listing pages show it."""
    return f"""<li class="row"><div class="col-lg-10"><div class="newsLink pl-0">
<div class="date-source"> <span>{when} ET</span> <span>|</span>
<span>Source: <a href="{page}?page=1" class="sourceLink">{organization}</a></span> </div>
<div class="mainLink"> <a href="/news-release/{link}/0/en/x.html">{title}</a> </div>
<div class="newsTxt"><p>NEW YORK (GLOBE NEWSWIRE) -- ...</p></div></div></div></li>"""


def listing(*rows: str) -> bytes:
    return f"<ul>{''.join(rows)}</ul>".encode()


def test_a_listing_gives_each_release_its_minute_organization_and_link() -> None:
    page = listing(
        row(
            "March 23, 2026 07:10",
            "WeRide Inc.",
            "2026/03/23/3260264",
            "WeRide Reports Record Full Year 2025 Revenue of RMB684.6 Million, Up 90%",
        ),
        row("November 24, 2025 04:00", "WeRide Inc.", "2025/11/24/3193234", "Global &amp; Growth"),
        # The same release in Hebrew
        row("November 24, 2025 04:00", "WeRide Inc.", "2025/11/24/3193300", "x").replace(
            "/0/en/", "/0/he/"
        ),
    ).decode()

    [(page_path, organization, march), (_, _, november)] = listed(page)

    assert (page_path, organization) == ("/en/search/organization/WeRide%2520Inc§", "WeRide Inc.")
    assert march.key == "3260264"
    assert march.url == "https://www.globenewswire.com/news-release/2026/03/23/3260264/0/en/x.html"
    assert datetime.fromisoformat(march.published or "") == datetime(2026, 3, 23, 7, 10, tzinfo=ET)
    assert november.title == "Global & Growth"
    assert datetime.fromisoformat(november.published or "") == datetime(2025, 11, 24, 4, tzinfo=ET)


async def test_a_companys_releases_come_from_its_own_organization_page() -> None:
    hesai = "/en/search/organization/Hesai%2520Group&#167;"
    search = listing(
        row("May 19, 2026 05:00", "Hesai Group", "2026/05/19/3297198", "Hesai results", hesai),
        row("May 13, 2026 05:40", "WeRide Inc.", "2026/05/13/3293762", "WeRide Q1"),
        row("March 23, 2026 07:10", "WeRide Inc.", "2026/03/23/3260264", "WeRide FY"),
    )
    first = listing(row("May 13, 2026 05:40", "WeRide Inc.", "2026/05/13/3293762", "WeRide Q1"))
    second = listing(
        row("November 24, 2025 04:00", "WeRide Inc.", "2025/11/24/3193234", "WeRide Q3")
    )
    fetcher = RoutedFetcher(
        {
            "/en/search/keyword/WeRide%20Inc?": (200, search),  # no period, which finds nothing
            "WeRide%2520Inc%C2%A7?page=1": (200, first),
            "WeRide%2520Inc%C2%A7?page=2": (200, second),
        }
    )

    releases = await past_releases(fetcher, "WeRide Inc.")

    assert [item.key for item in releases] == ["3293762", "3193234"]
    assert await past_releases(RoutedFetcher({}), "WeRide Inc.") == []


async def test_another_company_with_a_similar_name_isnt_taken_for_it() -> None:
    bam = "/en/search/organization/Brookfield%2520Asset%2520Management&#167;"
    search = listing(
        row(
            "November 06, 2026 07:04", "Brookfield Asset Management Ltd.", "2026/11/06/1", "Q3", bam
        ),
    )
    fetcher = RoutedFetcher(
        {
            "/en/search/keyword/BROOKFIELD%20Corp?": (200, search),
            "Brookfield%2520Asset%2520Management%C2%A7": (200, search),
        }
    )

    assert await past_releases(fetcher, "BROOKFIELD Corp /ON/") == []
