"""Writing an earnings brief: Muse Ember selects structured facts, code checks each one against
the release and lays out the ones it confirms."""

import hashlib
import json
from dataclasses import dataclass, fields
from typing import Any

import anthropic

from earnings_bot.config import Settings
from earnings_bot.source_document import SourceDocument, document_from_text
from earnings_bot.summary_binding import bind_answer
from earnings_bot.summary_bound_format import render_bound
from earnings_bot.summary_format import discord_brief
from earnings_bot.summary_policy import EMBER, SummaryFact
from earnings_bot.summary_request import referenced_request, structured_request
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
    evidence: str | None = None  # candidate, bound references and request provenance


class SummaryError(Exception):
    pass


def summarizer_from(settings: Settings) -> "Summarizer | None":
    """Live summaries cost money per release, so a key alone never turns them on."""
    if not settings.live_summaries or settings.meta_model_api_key is None:
        return None
    return Summarizer(
        settings.meta_model_api_key.get_secret_value(),
        references=settings.source_referenced_summaries,
    )


def _fact(line: dict[str, Any]) -> SummaryFact:
    return SummaryFact(
        **{
            name.name: str(line.get(name.name) or ("actual" if name.name == "kind" else ""))
            for name in fields(SummaryFact)
        }
    )


class Summarizer:
    def __init__(self, api_key: str, base_url: str = META_API, *, references: bool = False) -> None:
        self.references = references
        # One retry at most: a later source's text can start its own attempt
        self.client = anthropic.AsyncAnthropic(
            api_key=api_key,
            base_url=base_url,
            max_retries=0 if references else 1,
            timeout=TIMEOUT_SECONDS,
        )

    async def write_document(
        self,
        ticker: str,
        document: SourceDocument,
        seconds: float = 0.0,
        *,
        context: dict[str, Any] | None = None,
    ) -> Summary:
        """Reference binding is separately opt-in; default requests remain unchanged."""
        if getattr(self, "references", False):
            return await self._write_referenced(ticker, document, seconds, context)
        return (
            await self.write(ticker, document.text, seconds, context=context)
            if context
            else await self.write(ticker, document.text, seconds)
        )

    async def write(
        self,
        ticker: str,
        release: str,
        seconds: float = 0.0,
        *,
        context: dict[str, Any] | None = None,
    ) -> Summary:
        if getattr(self, "references", False):
            try:
                document = document_from_text(release)
            except ValueError as error:
                raise SummaryError(str(error)) from error
            return await self._write_referenced(ticker, document, seconds, context)
        # No consensus estimates: the tested setting had none, and the brief never calls a beat
        # or miss without a matching estimate
        try:
            request = structured_request(ticker, release, context)
        except ValueError as error:
            raise SummaryError(str(error)) from error
        message = await self._generate(request)
        answer = self._answer(message)
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

    async def _generate(self, request: dict[str, Any]) -> anthropic.types.Message:
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
        return message

    @staticmethod
    def _answer(message: anthropic.types.Message) -> str:
        return "".join(block.text for block in message.content if block.type == "text").strip()

    async def _write_referenced(
        self, ticker: str, document: SourceDocument, seconds: float, context: dict[str, Any] | None
    ) -> Summary:
        try:
            request = referenced_request(ticker, document, context)
        except ValueError as error:
            raise SummaryError(str(error)) from error
        message = await self._generate(request)
        if message.stop_reason != "end_turn":
            raise SummaryError(f"{WRITER} returned an unfinished reference candidate")
        answer = self._answer(message)
        try:
            report = bind_answer(answer, document)
        except ValueError as error:
            raise SummaryError(f"{WRITER} returned no usable source references") from error
        if not report.facts:
            raise SummaryError(f"none of {WRITER}'s facts could be bound to the source document")
        evidence = {
            "version": "bound-summary/1",
            "document_sha256": document.sha256,
            "request_sha256": hashlib.sha256(
                json.dumps(request, ensure_ascii=False, sort_keys=True).encode("utf-8")
            ).hexdigest(),
            "model": request["model"],
            "output_config": request["output_config"],
            "system": request["system"],
            "issuer_context": context or {},
            "candidate": answer,
            "accepted": [bound.fact.model_dump(mode="json") for bound in report.facts],
            "rejected": [
                {"metric": r.metric, "field": r.field, "reasons": r.reasons}
                for r in report.rejected
            ],
        }
        return Summary(
            text=render_bound(report),
            unverified=[f"{r.metric}.{r.field}: {'; '.join(r.reasons)}" for r in report.rejected],
            seconds=seconds,
            input_tokens=message.usage.input_tokens,
            output_tokens=message.usage.output_tokens,
            evidence=json.dumps(evidence, ensure_ascii=False),
        )
