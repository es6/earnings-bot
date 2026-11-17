"""Brookfield (BN), a 6-K filer, as SEC and its IR site show it, for tests of its watches."""

import json
from datetime import datetime

from first_print.recorder import ET
from tests.builders import sec_filing_detail

RESULT = "0001171843-26-005474"
ORDINARY = "0001171843-26-005640"
CIK = 1001085
HOST = "https://bn.brookfield.com"
LISTING = f"{HOST}/events-news/press-releases"
NOTICE = f"{HOST}/press-releases/third-quarter-results-conference-call"
TITLE = "Brookfield Corporation to Host Third Quarter 2026 Results Conference Call"
NOTICE_BODY = (
    f"<h1>{TITLE}</h1><p>Brookfield Corporation will host its third quarter 2026 conference call "
    "on Thursday, November 12, 2026, at 10:00 a.m. (ET).</p>"
    "<p>Results will be released that morning at approximately 7:00 a.m. (ET).</p>"
).encode()
RELEASE = (
    "<h1>Brookfield Corporation Reports Increase in Earnings</h1>"
    "<p>BROOKFIELD NEWS, Aug. 13, 2026 (GLOBE NEWSWIRE) -- Brookfield Corporation announced "
    "strong financial results for the quarter ended June 30, 2026.</p>"
    + "<p>Revenues of $5 billion and net income from operations of $300 million. </p>" * 35
    + "<p>Visit www.bn.brookfield.com for more information.</p>"
)
FINANCING = (
    "<h1>Brookfield Announces Closing of Note Offering</h1>"
    + "<p>The notes will mature in 2035 and finance company operations.</p>" * 40
    + "<p>Brookfield reported earnings for the last quarter. Revenues of $5 billion.</p>"
    + "<p>Net income grew.</p>"
)


def filings(*, accepted: str = "2026-08-13T10:45:04Z") -> dict:
    return {
        "fiscalYearEnd": "1231",
        "filings": {
            "recent": {
                "accessionNumber": [ORDINARY, RESULT],
                "form": ["6-K", "6-K"],
                "items": ["", ""],
                "acceptanceDateTime": [accepted, accepted],
                "primaryDocument": ["cover.htm", "cover.htm"],
            }
        },
    }


def folder(key: str) -> str:
    return f"/Archives/edgar/data/{CIK}/{key.replace('-', '')}"


def fixtures(data: dict) -> dict[str, tuple[int, bytes]]:
    routes = {f"CIK{CIK:010d}.json": (200, json.dumps(data).encode())}
    for key, html in [(ORDINARY, FINANCING), (RESULT, RELEASE)]:
        base = folder(key)
        routes[f"{base}/{key}-index.htm"] = (
            200,
            sec_filing_detail(f"{base}/cover.htm", "exh_991.htm"),
        )
        routes[f"{base}/exh_991.htm"] = (200, html.encode())
    return routes


# Brookfield files 6-Ks, so its dates come from the issuer's exhibits and its IR site
BN_NOW = datetime(2026, 11, 9, 17, tzinfo=ET)
