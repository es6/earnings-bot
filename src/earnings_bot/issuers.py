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

SEC_TICKERS = "https://www.sec.gov/files/company_tickers.json"
SEC_SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
TICKERS_REFRESH = 24 * 3600  # seconds; SEC rebuilds the list daily
_SYMBOL = re.compile(r"[A-Z0-9][A-Z0-9-]{0,9}")
# Legal-entity endings that headlines leave off: "Lucid Group, Inc." is "Lucid Group"
_ENTITY = re.compile(
    r"[,\s]+(?:inc|incorporated|corp|corporation|co|company|ltd|limited|plc|llc|lp|l\.p|n\.v|"
    r"s\.a|ag|se)\.?$",
    re.IGNORECASE,
)
_STATE = re.compile(r"\s*/\s*[A-Z]{2,}/?$")  # SEC's "Rivian Automotive, Inc. / DE"
_REGULAR_HOURS = (clock_time(9, 30), clock_time(16))
_ERRORS = (aiohttp.ClientError, TimeoutError, ValueError, KeyError, TypeError, AttributeError)


def symbol(ticker: str) -> str:
    """SEC's spelling of a ticker: BRK.B is BRK-B."""
    return ticker.strip().upper().replace(".", "-").replace("/", "-")


def wire_names(title: str, ticker: str) -> list[str]:
    """What identifies the company in a newswire item. Every wire release opens with the
    exchange and ticker, as in "Lucid Group, Inc. (NASDAQ: LCID)". The name alone counts only
    when it's more than one word, since "Block" or "Target" turn up in other companies' news."""
    name = _STATE.sub("", title).strip()
    while (shorter := _ENTITY.sub("", name)) != name:
        name = shorter
    tickers = dict.fromkeys([ticker, ticker.replace("-", ".")])
    tags = [f"{exchange}: {t}" for t in tickers for exchange in ("NASDAQ", "NYSE")]
    return ([name] if len(name.split()) > 1 else []) + tags


def release_time(accepted: list[datetime]) -> str | None:
    """The likely release time, from when SEC accepted recent results 8-Ks. The 8-K follows the
    release by a minute or two, so this is the most common session's middle time, rounded down
    to five minutes. Some item 2.02 filings aren't results, so the odd one out is ignored."""
    times = [moment.astimezone(ET).time() for moment in accepted]
    if not times:
        return None
    opens, closes = _REGULAR_HOURS
    sessions = [
        [t for t in times if t < opens],
        [t for t in times if opens <= t < closes],
        [t for t in times if t >= closes],
    ]
    # Ties go to the session of the newest filing
    newest = next(i for i, session in enumerate(sessions) if times[0] in session)
    busiest = max(range(3), key=lambda i: (len(sessions[i]), i == newest))
    chosen = sorted(sessions[busiest])
    middle = chosen[(len(chosen) - 1) // 2]
    return f"{middle.hour:02d}:{middle.minute - middle.minute % 5:02d}"


def profile(ticker: str, cik: int, title: str, submissions: dict, today: date) -> KnownCompany:
    """A catalog-style entry for a company nobody has researched yet."""
    recent = submissions["filings"]["recent"]
    accepted = [
        datetime.fromisoformat(recent["acceptanceDateTime"][i])
        for i, form in enumerate(recent["form"])
        if form == "8-K" and "2.02" in recent["items"][i].split(",")
    ][:4]
    year_end = submissions.get("fiscalYearEnd") or ""
    return KnownCompany(
        name=title,
        names=wire_names(title, ticker),
        cik=cik,
        platform="unknown",
        reachable=False,
        wire="unknown",
        release_time=release_time(accepted),
        eightk=[f"{moment.astimezone(ET):%H:%M}" for moment in accepted],
        fastest="unknown",
        evidence=f"Built from SEC data for {title}, not researched by hand",
        checked=today,
        fiscal_year_end=year_end if re.fullmatch(r"\d{4}", year_end) else None,
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
