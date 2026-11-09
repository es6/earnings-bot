"""SEC's answers for Lucid, a company outside the catalog, as of November 2026."""

import json

LCID_CIK = 1811210
TICKERS = {
    "0": {"cik_str": 1045810, "ticker": "NVDA", "title": "NVIDIA CORP"},
    "1": {"cik_str": LCID_CIK, "ticker": "LCID", "title": "Lucid Group, Inc."},
    "2": {"cik_str": 1067983, "ticker": "BRK-B", "title": "BERKSHIRE HATHAWAY INC"},
}
# Newest first, as SEC lists them: (form, items, accepted)
LCID_FILINGS = [
    ("8-K", "2.02,9.01", "2026-10-05T20:06:06.000Z"),  # deliveries, 4:06 PM
    ("10-Q", "", "2026-08-05T20:30:00.000Z"),
    ("8-K", "2.02,9.01", "2026-08-04T20:07:05.000Z"),  # Q2 results, 4:07 PM
    ("8-K/A", "2.02", "2026-07-10T12:00:00.000Z"),
    ("8-K", "2.02,5.02,7.01,9.01", "2026-07-02T13:01:58.000Z"),  # 9:01 AM
    ("8-K", "5.07", "2026-06-10T20:00:00.000Z"),
    ("8-K", "2.02,9.01", "2026-05-05T20:07:16.000Z"),  # Q1 results, 4:07 PM
    ("8-K", "1.01,2.02,3.02,9.01", "2026-04-14T11:07:17.000Z"),  # a fifth, too old to count
]
LCID_SUBMISSIONS = {
    "cik": str(LCID_CIK),
    "name": "Lucid Group, Inc.",
    "fiscalYearEnd": "1231",
    "filings": {
        "recent": {
            "form": [form for form, _, _ in LCID_FILINGS],
            "items": [items for _, items, _ in LCID_FILINGS],
            "acceptanceDateTime": [accepted for _, _, accepted in LCID_FILINGS],
        }
    },
}
SEC_TICKERS_ROUTE = "www.sec.gov/files/company_tickers.json"
LCID_ROUTE = f"data.sec.gov/submissions/CIK{LCID_CIK:010d}.json"


def sec_routes() -> dict[str, tuple[int, bytes]]:
    return {
        SEC_TICKERS_ROUTE: (200, json.dumps(TICKERS).encode()),
        LCID_ROUTE: (200, json.dumps(LCID_SUBMISSIONS).encode()),
    }
