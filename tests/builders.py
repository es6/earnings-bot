"""Shared test data builders and constants for the test modules."""

import io
import json
from datetime import UTC, date, datetime
from typing import Any

from earnings_bot.browser_sources import BrowserPDF
from earnings_bot.driver_profile import (
    parse_profile,
)
from earnings_bot.fetch import Response
from earnings_bot.investigate import INVESTIGATION
from earnings_bot.issuers import profile
from earnings_bot.known import KnownCompany, SourceCheck
from earnings_bot.publication_trials import TrialPlan
from earnings_bot.recorder import ET, Target
from earnings_bot.source_audit import Speed, Timing
from earnings_bot.source_document import document_from_html
from earnings_bot.sources import (
    Q4PressReleases,
    Sighting,
)
from tests.sec_data import LCID_CIK, LCID_SUBMISSIONS

AMAT_NOTICE_BODY = (
    "<h1>Applied Materials to Report Fourth Quarter and Fiscal 2026 Results on Nov. 12, 2026</h1>"
    "<p>Oct 22, 2026 at 7:30 AM EDT</p><p>SANTA CLARA, Calif., Oct. 22, 2026 (GLOBE NEWSWIRE) "
    "-- Applied Materials, Inc. today announced that it will hold its fourth quarter and fiscal "
    "2026 earnings conference call on Thursday, Nov. 12, 2026, at 4:30 p.m. ET / 1:30 p.m. PT."
    "</p><p>The call will be webcast live at: https://ir.appliedmaterials.com. A replay will be "
    "available on the same day by 8:00 p.m. ET / 5:00 p.m. PT.</p>"
)


AMAT = KnownCompany(
    name="APPLIED MATERIALS INC /DE",
    names=["APPLIED MATERIALS", "NASDAQ: AMAT"],
    cik=6951,
    platform="own-site",
    reachable=False,
    wire="GlobeNewswire",
    release_time="16:00",
    fiscal_year_end="1026",
    fastest="unknown",
    evidence="Investigated Nov 09",
    checked=date(2026, 11, 9),
)


TODAY = date(2026, 11, 13)


SOURCE = (
    "Acme sells subscriptions to its software. Subscription revenue increased 20%. "
    "The company expects full-year subscription revenue growth of 18% to 20%."
)


KPI = {"label": "Subscription revenue", "evidence": "Subscription revenue increased 20%."}


def content(**updates: object) -> str:
    return json.dumps(
        {
            "business": {
                "label": "software",
                "evidence": "Acme sells subscriptions to its software.",
            },
            "segments": [],
            "kpis": [KPI],
            "guidance": [],
            "themes": [],
        }
        | updates
    )


def parsed(answer: str | None = None, published: date = TODAY):
    return parse_profile(
        answer or content(),
        SOURCE,
        source_url="https://issuer.example/results",
        document_sha256="a" * 64,
        source_period="FY2026Q3",
        source_published=published,
        today=TODAY,
    )


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


IR_COVERAGE_RESULTS = "https://www.sea.com/api/financial/resource"


NEWS = "https://www.sea.com/api/invest/news?page=1&size=12"


IR_COVERAGE_NOW = datetime(2026, 11, 9, 12, tzinfo=ET)


def company(ticker: str) -> KnownCompany:
    return KnownCompany(
        name="Sea Limited" if ticker == "SE" else "On Holding AG",
        cik=1703399 if ticker == "SE" else 1858985,
        platform="own-site",
        reachable=False,
        wire="unknown",
        sec_form="6-K",
        release_time="06:10",
        fastest="unknown",
        evidence="Saved public source replay",
        checked=IR_COVERAGE_NOW.date(),
        investigated=IR_COVERAGE_NOW.date(),
        investigation=INVESTIGATION,
        checks=[SourceCheck(source="SEC", watch=True, why="Verified 6-K filer")],
        ir_news_api=NEWS if ticker == "SE" else None,
        ir_results_api=IR_COVERAGE_RESULTS if ticker == "SE" else None,
    )


MC = "https://corporate.mcdonalds.com/"


PDF_ONLY = MC + "release.pdf"


IR_PAGE = "https://investor.uber.com/news/uber-announces-results/default.aspx"


def uber() -> Target:
    return Target(ticker="UBER", expected=datetime(2026, 11, 4, 6, 55, tzinfo=ET), cik=1543151)


ACCESSION = "0001543151-26-000040"


FILING_INDEX = f"{ACCESSION}-index.htm"


def filing_routes(exhibit: str = "ex99-1.htm") -> dict[str, tuple[int, bytes]]:
    """The filing's file lists, primary document and press release exhibit."""
    items = [{"name": PRIMARY, "size": "90000"}, {"name": exhibit, "size": "50000"}]
    paragraph = "<p>Uber Announces Results for Third Quarter 2026. Gross bookings grew 21%.</p>"
    return {
        FILING_INDEX: (
            200,
            sec_filing_detail(PRIMARY, exhibit).replace(
                b"</table>",
                b"<tr><td>3</td><td>Press release announcing cash dividend</td>"
                b"<td>dividend-release.htm</td><td>EX-99.2</td><td>100000</td></tr></table>",
            ),
        ),
        "index.json": (200, json.dumps({"directory": {"item": items}}).encode()),
        PRIMARY: (200, ("<p>" + "Form 8-K cover text. " * 120 + "</p>").encode()),
        exhibit: (200, f"<html><body>{paragraph * 40}</body></html>".encode()),
    }


RELEASE_LINES = [
    "Uber Announces Results for Third Quarter 2026",
    *["Revenue grew 20% and net income was $1.2 billion, or $0.56 per share." for _ in range(40)],
]


def pdf_sighting() -> tuple[BrowserPDF, Sighting]:
    source = BrowserPDF(PDF_ONLY, MC)
    return source, source.parse(Response(PDF_ONLY, 200, {}, simple_pdf(RELEASE_LINES), 0.04))[0]


def ir_sighting(url: str = IR_PAGE) -> tuple[Q4PressReleases, Sighting]:
    return Q4PressReleases("investor.uber.com"), Sighting("1700", "Results", url, earnings=True)


def plan(**changes: Any) -> TrialPlan:
    route = {
        "transport": "http",
        "interval": 0.125,
        "issuer_pattern": "Example Corp",
        "period_pattern": "third quarter",
        "result_pattern": "results",
    }
    return TrialPlan.model_validate(
        {
            "id": "example-q3",
            "starts_at": datetime.now(UTC).isoformat(),
            "duration_seconds": 8,
            "max_requests": 4,
            "routes": [
                route
                | {"name": "listing", "url": "https://ir.example.com/news", "kind": "listing"},
                route
                | {
                    "name": "article",
                    "url": "https://ir.example.com/news/q3-2026",
                    "kind": "article",
                },
            ],
        }
        | changes
    )


def events(log: io.StringIO, name: str) -> list[dict[str, Any]]:
    return [
        row for line in log.getvalue().splitlines() if (row := json.loads(line))["event"] == name
    ]


TABLE = """<p>Q3 2026 (USD millions)</p><table>
<tr><th>Metric</th><th>Q3 2026</th><th>Q3 2025</th></tr>
<tr><th scope="row">Revenue</th><td>100</td><td>80</td></tr>
<tr><th scope="row">Operating income</th><td>10</td><td>8</td></tr></table>"""


DOCUMENT = document_from_html(
    "<p>Q3 2026 (USD millions)</p><table><tr><th>Metric</th><th>Q3 2026</th>"
    '<th>Q3 2025</th></tr><tr><th scope="row">Revenue</th><td>100</td><td>80</td></tr>'
    "</table><p>Revenue growth was 25% reported, 20% excluding acquisitions.</p>"
)


def evidence(**changes: Any) -> dict[str, Any]:
    return {
        "document_sha256": DOCUMENT.sha256,
        "block_id": "t1",
        "cell_ids": ["t1r1c0", "t1r0c1", "t1r1c1"],
        "quote": "Revenue | 100 | 80",
    } | changes


def value(**changes: Any) -> dict[str, Any]:
    return {
        "amount": "100",
        "upper": None,
        "unit": "money",
        "scale": "million",
        "currency": "USD",
        "value_qualifier": "exact",
        "period": "Q3 2026",
        "duration": "three months",
        "basis": "reported",
        "comparison": "",
        "comparison_qualifier": "",
        "evidence": [evidence()],
    } | changes


def fact(**changes: Any) -> dict[str, Any]:
    return {
        "metric": "Revenue",
        "kind": "actual",
        "role": "headline",
        "group": "",
        "relevance": "Headline growth",
        "current_actual": value(),
        "prior_year_actual": None,
        "reported_growth": None,
        "comparable_growth": None,
        "previous_guidance": None,
        "new_guidance": None,
        "update": "",
        "update_evidence": [],
    } | changes


PROGRESS_NOW = datetime(2026, 11, 9, 15, tzinfo=UTC)


LUCID = profile("LCID", LCID_CIK, "Lucid Group, Inc.", LCID_SUBMISSIONS, date(2026, 11, 9))


LISTING = "https://ir.lucidmotors.com/press-releases"


# What an investigation of Lucid finds
INVESTIGATED = LUCID.model_copy(
    update={
        "investigated": date(2026, 11, 9),
        "investigation": INVESTIGATION,
        "wire": "PR Newswire",
        "ir_listing": LISTING,
        "ir_http_status": 403,
        "platform": "own-site",
        "checks": [
            SourceCheck(source="SEC 8-K filings", watch=True, why="Filed here"),
            SourceCheck(
                source="ir.lucidmotors.com without Chrome",
                watch=False,
                why="Blocks automated requests (HTTP 403)",
            ),
            SourceCheck(
                source=f"Press release page <{LISTING}>, read in Chrome",
                watch=True,
                why="Chrome read its latest results",
            ),
            SourceCheck(
                source="Business Wire earnings feed",
                watch=False,
                why="Its releases go out on PR Newswire",
            ),
        ],
    }
)


SPEED = Speed(
    date(2026, 8, 5),
    confident=True,
    measured_headlines=[
        Timing("edgar_latest", datetime(2026, 8, 5, 16, 8, 53, tzinfo=ET)),
        Timing("prnewswire_busted", datetime(2026, 8, 5, 16, 5, 17, tzinfo=ET)),
        Timing("prnewswire", datetime(2026, 8, 5, 16, 6, 2, tzinfo=ET)),
        Timing("browser-ir", datetime(2026, 8, 5, 16, 6, 59, tzinfo=ET)),
    ],
    benchmark="[@earnings_guy posted](https://x.com/earnings_guy/status/1) at 16:06:00 ET",
)


GUILD = 555


TRACKING_NOW = datetime(2026, 11, 12, 12, tzinfo=ET)


NVDA_CIK = 1045810


OWNER = 4242  # owns the bot's Discord application


PLTR_FEED = "investors.palantir.com/feed/PressRelease.svc/GetPressReleaseList"


TRACKING_RESULTS = {
    "PressReleaseId": 9001,
    "Headline": "Palantir Reports Q4 2026 Financial Results",
    "LinkToDetailPage": "/news-details/2027/palantir-reports-q4-2026",
    "PressReleaseDate": "02/02/2027 16:05:00",
    "Body": "<p>" + "Revenue grew 70% year over year to $1.4 billion. " * 40 + "</p>",
}


def sec_filing_detail(primary: str, exhibit: str) -> bytes:
    return f'''
<table summary="Document Format Files">
<tr><th>Seq</th><th>Description</th><th>Document</th><th>Type</th><th>Size</th></tr>
<tr><td>1</td><td>Current report</td><td><a href="/ix?doc={primary}">{primary}</a>
    <span>iXBRL</span></td><td>8-K</td><td>90000</td></tr>
<tr><td>2</td><td></td><td><a href="{exhibit}">{exhibit}</a></td><td>EX-99.1</td>
    <td>50000</td></tr>
</table>'''.encode()


def simple_pdf(lines: list[str]) -> bytes:
    """A one-page PDF with these lines of text, built by hand so tests need no PDF writer."""
    shown = " T* ".join(
        "(" + line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)") + ") Tj"
        for line in lines
    )
    stream = f"BT /F1 10 Tf 12 TL 40 760 Td {shown} ET".encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
            b"/Resources << /Font << /F1 5 0 R >> >> >>"
        ),
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    data = b"%PDF-1.4\n"
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(data))
        data += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(data)
    data += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    data += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    data += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref,
    )
    return data


FILING = "https://www.sec.gov/Archives/edgar/data/1543151/000154315126000040/uber-20261104.htm"
FOLDER, PRIMARY = FILING.rsplit("/", 1)
