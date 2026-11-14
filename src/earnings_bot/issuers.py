"""Company facts for any ticker SEC lists, so /track isn't limited to the researched catalog."""

import json
import re
import time
from datetime import date, datetime
from datetime import time as clock_time

import aiohttp

from earnings_bot.fetch import Fetcher
from earnings_bot.known import KnownCompany, known_companies
from earnings_bot.recorder import ET
from earnings_bot.sources import SecForm, issuer_wire_name

SEC_TICKERS = "https://www.sec.gov/files/company_tickers.json"
SEC_SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
TICKERS_REFRESH = 24 * 3600  # seconds; SEC rebuilds the list daily
_SYMBOL = re.compile(r"[A-Z0-9][A-Z0-9-]{0,9}")
_REGULAR_HOURS = (clock_time(9, 30), clock_time(16))
_ERRORS = (aiohttp.ClientError, TimeoutError, ValueError, KeyError, TypeError, AttributeError)


def symbol(ticker: str) -> str:
    """SEC's spelling of a ticker: BRK.B is BRK-B."""
    return ticker.strip().upper().replace(".", "-").replace("/", "-")


def wire_names(title: str, ticker: str) -> list[str]:
    """What identifies the company in a newswire item. Every wire release opens with the
    exchange and ticker, as in "Lucid Group, Inc. (NASDAQ: LCID)". The name alone counts only
    when it's more than one word, since "Block" or "Target" turn up in other companies' news."""
    name = issuer_wire_name(title)
    tickers = dict.fromkeys([ticker, ticker.replace("-", ".")])
    tags = [f"{exchange}: {t}" for t in tickers for exchange in ("NASDAQ", "NYSE")]
    return ([name] if len(name.split()) > 1 else []) + tags


def _busiest_session(moments: list[datetime]) -> list[clock_time]:
    """The times of day, sorted, in the market session most of the moments fall in: before the
    open, during it or after the close. Ties go to the session of the newest, which comes first."""
    times = [moment.astimezone(ET).time() for moment in moments]
    if not times:
        return []
    opens, closes = _REGULAR_HOURS
    sessions = [
        [t for t in times if t < opens],
        [t for t in times if opens <= t < closes],
        [t for t in times if t >= closes],
    ]
    newest = next(i for i, session in enumerate(sessions) if times[0] in session)
    busiest = max(range(3), key=lambda i: (len(sessions[i]), i == newest))
    return sorted(sessions[busiest])


def _five_minutes(moment: clock_time) -> str:
    return f"{moment.hour:02d}:{moment.minute - moment.minute % 5:02d}"


def release_time(accepted: list[datetime]) -> str | None:
    """Estimate time from verified filings, favoring the most common market session."""
    chosen = _busiest_session(accepted)
    return _five_minutes(chosen[(len(chosen) - 1) // 2]) if chosen else None


# How many recent quarterly releases a company's usual time comes from, and the fewest it needs:
# with one, the last release places the window as before
USUAL_RELEASES = 4
USUAL_FEWEST = 2


def usual_release_time(published: list[datetime]) -> str | None:
    """The earliest time of day among the company's last USUAL_RELEASES quarterly releases,
    newest first, in the session most of them came out in, or None with fewer than USUAL_FEWEST.
    An early estimate costs some slow polling before the release; a late one can leave its own
    site slow, or the window shut, when it comes out. Backtested Nov 14 on 199 past quarterly
    releases of 31 companies, timed by their IR archives, each placed from the releases before
    it: 189 on time, against 182 by the last release alone, and 5 late inside the window against
    11 (scratchpad timing_rescore.py)."""
    recent = published[:USUAL_RELEASES]
    if len(recent) < USUAL_FEWEST:
        return None
    chosen = _busiest_session(recent)
    return _five_minutes(chosen[0]) if chosen else None


def filing_form(submissions: dict) -> SecForm:
    forms = set(submissions["filings"]["recent"]["form"])
    if "6-K" in forms and (forms & {"20-F", "40-F"} or "8-K" not in forms):
        return "6-K"
    return "8-K"


def fiscal_year_end(submissions: dict) -> str | None:
    """SEC's MMDD for the company's fiscal year end, or else the end of the period its latest
    annual report covers. SEC leaves the field empty for some, such as Alibaba."""
    year_end = submissions.get("fiscalYearEnd") or ""
    if re.fullmatch(r"\d{4}", year_end):
        return year_end
    recent = submissions.get("filings", {}).get("recent", {})
    for form, period in zip(recent.get("form", []), recent.get("reportDate", []), strict=False):
        if form in ("10-K", "20-F", "40-F") and re.fullmatch(r"\d{4}-\d{2}-\d{2}", period or ""):
            return period[5:7] + period[8:10]
    return None


def profile(ticker: str, cik: int, title: str, submissions: dict, today: date) -> KnownCompany:
    """A catalog-style entry for a company nobody has researched yet."""
    recent = submissions["filings"]["recent"]
    accepted = [
        datetime.fromisoformat(recent["acceptanceDateTime"][i])
        for i, form in enumerate(recent["form"])
        if form == "8-K" and "2.02" in recent["items"][i].split(",")
    ][:4]
    return KnownCompany(
        name=title,
        names=wire_names(title, ticker),
        cik=cik,
        platform="unknown",
        reachable=False,
        wire="unknown",
        sec_form=filing_form(submissions),
        release_time=release_time(accepted),
        eightk=[f"{moment.astimezone(ET):%H:%M}" for moment in accepted],
        fastest="unknown",
        evidence=f"Built from SEC data for {title}, not researched by hand",
        checked=today,
        fiscal_year_end=fiscal_year_end(submissions),
    )


class Issuers:
    """Finds the company behind a ticker: the catalog's entry, or one built from SEC data."""

    def __init__(self, fetcher: Fetcher) -> None:
        self.fetcher = fetcher
        self._listed: dict[str, tuple[int, str]] = {}
        self._listed_until = 0.0

    async def company(self, ticker: str, today: date) -> KnownCompany:
        """Raises ValueError, with a reply for the member, when there's no such company."""
        if known := known_companies().get(ticker):
            return known
        if not _SYMBOL.fullmatch(ticker):
            raise ValueError("Enter a ticker symbol, such as NVDA.")
        unavailable = ValueError("SEC's company data didn't load. Try again in a minute.")
        try:
            listed = await self._tickers()
        except _ERRORS:
            raise unavailable from None
        if ticker not in listed:
            raise ValueError(f"{ticker} isn't on SEC's list of company tickers.")
        cik, title = listed[ticker]
        try:
            reply = await self.fetcher.request(
                "GET", SEC_SUBMISSIONS.format(cik=cik), give_up_after=8
            )
            if reply.status != 200:
                raise ValueError(f"HTTP {reply.status}")
            return profile(ticker, cik, title, json.loads(reply.body), today)
        except _ERRORS:
            raise unavailable from None

    async def _tickers(self) -> dict[str, tuple[int, str]]:
        if time.monotonic() >= self._listed_until:
            reply = await self.fetcher.request("GET", SEC_TICKERS, give_up_after=8)
            if reply.status != 200:
                raise ValueError(f"HTTP {reply.status}")
            self._listed = {
                row["ticker"]: (int(row["cik_str"]), row["title"])
                for row in json.loads(reply.body).values()
            }
            self._listed_until = time.monotonic() + TICKERS_REFRESH
        return self._listed
