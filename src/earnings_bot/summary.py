"""Writing a compact earnings brief and flagging numbers to review."""

import re
from dataclasses import dataclass

import anthropic

from earnings_bot.estimates import Estimates
from earnings_bot.summary_policy import SONNET, TEXT_SYSTEM

MODEL = SONNET.model
EFFORT = SONNET.effort
SYSTEM = TEXT_SYSTEM


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
            # Use the selected model only; implicit provider fallback changes cost/quality.
            async with self.client.messages.stream(
                model=MODEL,
                max_tokens=16000,
                system=SYSTEM,
                messages=[{"role": "user", "content": prompt}],
                output_config={"effort": EFFORT},
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
