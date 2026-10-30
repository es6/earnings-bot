"""Writing the earnings post with Claude, and checking its numbers against the release."""

import re
from dataclasses import dataclass

import anthropic

from earnings_bot.estimates import Estimates

MODEL = "claude-opus-5-5"
# Medium is Opus 5.5's default. Lower it for speed if the posts hold up.
EFFORT = "medium"

SYSTEM = """\
You write earnings posts for a Discord channel of traders. You get one company's earnings \
release and, when available, consensus estimates. Write the post in the house style below.

Accuracy comes first:
- Every number must come from the release or from the estimates you're given. Round the way \
the style shows ($14.21B, $0.84, 31.4%), but don't work out new numbers, except year-over-year \
growth when the release gives both periods without stating the growth.
- Leave out anything the release doesn't say. Never guess an estimate or compare against one \
you weren't given.
- Use the company's headline EPS (adjusted or non-GAAP when it reports one) and say which it is.

Style:
- First line: `COMPANY NAME $TICKER Q3'26 EARNINGS HIGHLIGHTS`, the name in capitals and the \
quarter as the company labels it (`FISCAL Q4'26` when its fiscal year isn't the calendar year).
- Then a blank line and one headline line per key number, starting with 🔹: EPS, revenue, and \
the one or two numbers investors in this company watch most.
- After a number with an estimate, add `(Est. ...)` and a mark: 🟢 beat, 🔴 miss, 🟡 within \
about 1%. No mark without an estimate.
- Then 3 to 5 short sections, each a title ending in a colon followed by 2 to 6 🔹 lines: \
guidance first if the company gives any, then the company's key operating metrics (segments, \
users, bookings, margins, whatever matters for this business), then capital return if any.
- End with `Comments:` and one or two short executive quotes, copied exactly, as \
`🔸 "..." – Title Name` lines.
- Keep it under 3,000 characters. Write only the post: no preamble and no markdown headings.

Example, for a made-up company:

NORTHWIND ROBOTICS $NWR Q2'26 EARNINGS HIGHLIGHTS

🔹 Adj. EPS: $1.12 (Est. $1.05) 🟢
🔹 Revenue: $3.48B (Est. $3.51B) 🟡; +9% YoY
🔹 Backlog: $9.8B; +15% YoY

Q3 Guide:
🔹 Revenue: $3.55B–$3.65B
🔹 Adj. EPS: $1.15–$1.20

Segment Revenue:
🔹 Warehouse Systems: $2.10B; +12% YoY
🔹 Field Robotics: $1.02B; +4% YoY
🔹 Services: $360M; +7% YoY

Other Metrics:
🔹 Gross Margin: 41.2% vs 39.8% last year
🔹 Free Cash Flow: $410M

Capital Return:
🔹 Repurchased $250M of stock
🔹 Quarterly dividend raised 8% to $0.27

Comments:
🔸 "Demand for our warehouse systems has never been stronger." – CEO Dana Ruiz"""  # noqa: RUF001 (en dashes are the house style)


@dataclass(frozen=True)
class Summary:
    text: str
    unverified: list[str]  # numbers in the post that couldn't be found in the release
    seconds: float
    input_tokens: int
    output_tokens: int


class SummaryError(Exception):
    pass


class Summarizer:
    def __init__(self, api_key: str) -> None:
        self.client = anthropic.AsyncAnthropic(api_key=api_key)

    async def write(
        self, ticker: str, release: str, estimates: Estimates | None, seconds: float = 0.0
    ) -> Summary:
        prompt = (
            f"Ticker: {ticker}\n\n"
            f"Consensus estimates:\n{estimates.describe() if estimates else 'None available'}\n\n"
            f"<release>\n{release}\n</release>"
        )
        try:
            # Releases run to tens of thousands of tokens, so stream to stay clear of timeouts.
            # If a safety classifier declines, the API retries on a fallback model by itself.
            async with self.client.beta.messages.stream(
                model=MODEL,
                max_tokens=16000,
                system=SYSTEM,
                messages=[{"role": "user", "content": prompt}],
                output_config={"effort": EFFORT},
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            ) as stream:
                message = await stream.get_final_message()
        except anthropic.APIStatusError as error:
            raise SummaryError(f"Claude API error {error.status_code}: {error.message}") from error
        except anthropic.APIConnectionError as error:
            raise SummaryError(f"couldn't reach the Claude API: {error}") from error
        if message.stop_reason == "refusal":
            raise SummaryError("Claude declined to write this summary")
        text = "".join(block.text for block in message.content if block.type == "text").strip()
        if not text:
            raise SummaryError(f"Claude returned no text (stop reason {message.stop_reason})")
        return Summary(
            text=text,
            unverified=unverified_numbers(text, release, estimates),
            seconds=seconds,
            input_tokens=message.usage.input_tokens,
            output_tokens=message.usage.output_tokens,
        )


# A number as the post writes it: "$14.21B", "0.84", "31.4%", "+18%", "1,250", but not the
# digits in labels like "FY26" or "Q3'26"
_NUMBER = re.compile(r"(?<![\w'.,])(\d[\d,]*(?:\.\d+)?)\s*(T|B|M|K|bps|%)?(?![\w'])")
_SCALES = {"T": 1e12, "B": 1e9, "M": 1e6, "K": 1e3}


def _source_numbers(text: str) -> list[float]:
    return [float(match.replace(",", "")) for match in re.findall(r"\d[\d,]*(?:\.\d+)?", text)]


def unverified_numbers(post: str, release: str, estimates: Estimates | None) -> list[str]:
    """Numbers in the post that don't match any number in the release or the estimates, after
    allowing for rounding and for tables in thousands or millions. Year-over-year growth that the
    release doesn't state outright shows up here too, so treat this as a list to check, not a
    list of errors."""
    known = _source_numbers(release)
    if estimates:
        known += [value for value in (estimates.eps, estimates.revenue) if value is not None]
    unverified = []
    for line in post.splitlines()[1:]:  # the header is all quarter labels and tickers
        for match in _NUMBER.finditer(line):
            digits, unit = match.group(1), match.group(2)
            value = float(digits.replace(",", ""))
            if value < 10 and unit is None and "." not in digits:
                continue  # counts like "2 new products" and list numbers
            decimals = len(digits.split(".")[1]) if "." in digits else 0
            tolerance = 0.5 * 10**-decimals
            if unit in _SCALES:
                scale = _SCALES[unit]
                # Releases state big numbers in dollars, thousands, millions or billions
                candidates = [value * scale / s for s in (1, 1e3, 1e6, 1e9)]
                tolerances = [tolerance * scale / s for s in (1, 1e3, 1e6, 1e9)]
            else:
                candidates, tolerances = [value], [tolerance]
            if not any(
                abs(number - candidate) <= tol + 1e-9
                for candidate, tol in zip(candidates, tolerances, strict=True)
                for number in known
            ):
                unverified.append(match.group(0).strip())
    return unverified
