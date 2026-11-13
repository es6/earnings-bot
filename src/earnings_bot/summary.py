"""Writing an earnings brief: Muse Ember selects structured facts, code checks each one against
the release and lays out the ones it confirms."""

import json
from dataclasses import dataclass, fields
from typing import Any

import anthropic

from earnings_bot.config import Settings
from earnings_bot.source_document import SourceDocument
from earnings_bot.summary_format import discord_brief
from earnings_bot.summary_policy import EMBER, SummaryFact
from earnings_bot.summary_request import structured_request
from earnings_bot.summary_validation import validate_facts

MODEL = EMBER.model
EFFORT = EMBER.effort
WRITER = "Muse Ember"
# Meta's Model API speaks the Anthropic Messages format at its own address
META_API = "https://api.meta.ai"
# Ember low took about 5 s on the tested releases; a hung request must not hold the summary
TIMEOUT_SECONDS = 120.0


@dataclass(frozen=True)
class Summary:
    text: str
    unverified: list[str]  # facts held back because the release didn't confirm them
    seconds: float
    input_tokens: int
    output_tokens: int


class SummaryError(Exception):
    pass


def summarizer_from(settings: Settings) -> "Summarizer | None":
    """Live summaries cost money per release, so a key alone never turns them on."""
    if not settings.live_summaries or settings.meta_model_api_key is None:
        return None
    return Summarizer(settings.meta_model_api_key.get_secret_value())


def _fact(line: dict[str, Any]) -> SummaryFact:
    return SummaryFact(
        **{
            name.name: str(line.get(name.name) or ("actual" if name.name == "kind" else ""))
            for name in fields(SummaryFact)
        }
    )


class Summarizer:
    def __init__(self, api_key: str, base_url: str = META_API) -> None:
        # One retry at most: a later source's text can start its own attempt
        self.client = anthropic.AsyncAnthropic(
            api_key=api_key, base_url=base_url, max_retries=1, timeout=TIMEOUT_SECONDS
        )

    async def write_document(
        self, ticker: str, document: SourceDocument, seconds: float = 0.0
    ) -> Summary:
        """Keep the existing request until the reference schema/verifier is ready."""
        return await self.write(ticker, document.text, seconds)

    async def write(self, ticker: str, release: str, seconds: float = 0.0) -> Summary:
        # No consensus estimates: the tested setting had none, and the brief never calls a beat
        # or miss without a matching estimate
        try:
            request = structured_request(ticker, release)
        except ValueError as error:
            raise SummaryError(str(error)) from error
        try:
            # Releases run to tens of thousands of tokens, so stream to stay clear of timeouts
            async with self.client.messages.stream(**request) as stream:
                message = await stream.get_final_message()
        except anthropic.APIStatusError as error:
            reason = f"{WRITER} API error {error.status_code}: {error.message}"
            raise SummaryError(reason) from error
        except anthropic.APIConnectionError as error:
            raise SummaryError(f"couldn't reach the {WRITER} API: {error}") from error
        if message.stop_reason == "refusal":
            raise SummaryError(f"{WRITER} declined to write this summary")
        answer = "".join(block.text for block in message.content if block.type == "text").strip()
        try:
            lines = json.loads(answer)["lines"]
            facts = [_fact(line) for line in lines if isinstance(line, dict)]
        except (json.JSONDecodeError, KeyError, TypeError) as error:
            raise SummaryError(
                f"{WRITER} returned no usable facts (stop reason {message.stop_reason})"
            ) from error
        accepted, rejected = validate_facts(facts, release)
        if not accepted:
            raise SummaryError(
                f"none of {WRITER}'s {len(facts)} facts could be confirmed in the release"
            )
        return Summary(
            text=discord_brief(accepted),
            unverified=rejected,
            seconds=seconds,
            input_tokens=message.usage.input_tokens,
            output_tokens=message.usage.output_tokens,
        )
