"""Writing an earnings brief: Muse Ember selects structured facts, code checks each one against
the release and lays out the ones it confirms."""

import asyncio
import hashlib
import json
from dataclasses import asdict, dataclass, fields, replace
from typing import Any

import anthropic

from first_print.config import Settings
from first_print.consensus import attach_estimates
from first_print.source_document import SourceDocument, document_from_text
from first_print.summary_binding import bind_answer
from first_print.summary_bound_format import render_bound
from first_print.summary_format import discord_brief
from first_print.summary_policy import EMBER, SummaryFact
from first_print.summary_request import referenced_request, structured_request
from first_print.summary_validation import validate_facts
from first_print.summary_verify import verify_facts

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
    unverified: list[str]  # validation refusals and trimmed components, not proven source errors
    seconds: float
    input_tokens: int
    output_tokens: int
    evidence: str | None = None  # candidate, bound references and request provenance
    validation: str = "legacy"


class SummaryError(Exception):
    def __init__(self, message: str, *, evidence: str | None = None) -> None:
        super().__init__(message)
        self.evidence = evidence


def _document_evidence(evidence: str | None, document: SourceDocument) -> str | None:
    if evidence is None:
        return None
    data = json.loads(evidence)
    data["document_sha256"] = document.sha256
    return json.dumps(data, ensure_ascii=False)


def summarizer_from(settings: Settings) -> "Summarizer | None":
    """Live summaries cost money per release, so a key alone never turns them on."""
    if not settings.live_summaries or settings.meta_model_api_key is None:
        return None
    return Summarizer(
        settings.meta_model_api_key.get_secret_value(),
        references=settings.source_referenced_summaries,
        verified=settings.verified_summaries,
    )


def _fact(line: dict[str, Any]) -> SummaryFact:
    return SummaryFact(
        **{
            name.name: str(line.get(name.name) or ("actual" if name.name == "kind" else ""))
            for name in fields(SummaryFact)
            if name.name not in ("estimate", "estimate_result")
        }
    )


class Summarizer:
    references = False
    verified = False

    def __init__(
        self,
        api_key: str,
        base_url: str = META_API,
        *,
        references: bool = False,
        verified: bool = False,
    ) -> None:
        self.references = references
        self.verified = verified
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
        if self.references:
            return await self._write_referenced(ticker, document, seconds, context)
        if self.verified:
            # The release as read, tables included, is what the facts are checked against
            return await self._write_facts(ticker, document.text, seconds, context, document)
        try:
            result = (
                await self.write(ticker, document.text, seconds, context=context)
                if context
                else await self.write(ticker, document.text, seconds)
            )
        except SummaryError as error:
            error.evidence = _document_evidence(error.evidence, document)
            raise
        return replace(result, evidence=_document_evidence(result.evidence, document))

    async def write(
        self,
        ticker: str,
        release: str,
        seconds: float = 0.0,
        *,
        context: dict[str, Any] | None = None,
    ) -> Summary:
        if self.references:
            try:
                document = document_from_text(release)
            except ValueError as error:
                raise SummaryError(str(error)) from error
            return await self._write_referenced(ticker, document, seconds, context)
        return await self._write_facts(ticker, release, seconds, context, None)

    async def _write_facts(
        self,
        ticker: str,
        release: str,
        seconds: float,
        context: dict[str, Any] | None,
        document: SourceDocument | None,
    ) -> Summary:
        # Estimates never enter the model prompt. Only verified facts can receive an exact,
        # deterministic comparison from a snapshot fetched before this release's window.
        snapshot = (context or {}).get("consensus_snapshot")
        writer_context = {k: v for k, v in (context or {}).items() if k != "consensus_snapshot"}
        try:
            request = structured_request(ticker, release, writer_context or None)
        except ValueError as error:
            raise SummaryError(str(error)) from error
        message = await self._generate(request)
        answer = self._answer(message)
        source = document or document_from_text(release)
        evidence: dict[str, Any] = {
            "version": "fact-summary/1",
            "document_sha256": source.sha256,
            "source_text_sha256": hashlib.sha256(release.encode("utf-8")).hexdigest(),
            "request_sha256": hashlib.sha256(
                json.dumps(request, ensure_ascii=False, sort_keys=True).encode("utf-8")
            ).hexdigest(),
            "model": request["model"],
            "effort": request["output_config"]["effort"],
            "raw_answer": answer,
            "stop_reason": message.stop_reason,
            "accepted": [],
            "rejected": [],
        }

        def failed(reason: str) -> SummaryError:
            return SummaryError(reason, evidence=json.dumps(evidence, ensure_ascii=False))

        if message.stop_reason != "end_turn":
            raise failed(f"{WRITER} returned an unfinished or refused fact answer")
        try:
            lines = json.loads(answer)["lines"]
            facts = [_fact(line) for line in lines if isinstance(line, dict)]
        except (json.JSONDecodeError, KeyError, TypeError) as error:
            raise failed(
                f"{WRITER} returned no usable facts (stop reason {message.stop_reason})"
            ) from error
        # Verified mode binds every kept figure to the release, so a badly quoted one can try
        accepted, rejected = validate_facts(facts, release, unquoted=self.verified)
        evidence["rejected"] = rejected
        if not accepted:
            raise failed(f"none of {WRITER}'s {len(facts)} facts could be confirmed in the release")
        if self.verified:
            # Up to about a second of parsing on a large release (measured 0.26 s median):
            # off the event loop, which also carries other releases' alerts
            accepted, dropped = await asyncio.to_thread(verify_facts, accepted, source)
            rejected = [*rejected, *dropped]
            evidence["rejected"] = rejected
            if not accepted:
                raise failed(
                    f"the release confirmed none of {WRITER}'s facts' measure, period and basis"
                )
        evidence["accepted"] = [asdict(fact) for fact in accepted]
        if self.verified and isinstance(snapshot, dict):
            accepted = await asyncio.to_thread(
                attach_estimates, accepted, source, snapshot, ticker=ticker
            )
            evidence["consensus_snapshot"] = snapshot
            evidence["displayed"] = [asdict(fact) for fact in accepted]
        return Summary(
            text=discord_brief(accepted),
            unverified=rejected,
            seconds=seconds,
            input_tokens=message.usage.input_tokens,
            output_tokens=message.usage.output_tokens,
            evidence=json.dumps(evidence, ensure_ascii=False),
            validation="source-values/1" if self.verified else "quote-check/1",
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
        return message

    @staticmethod
    def _answer(message: anthropic.types.Message) -> str:
        return "".join(block.text for block in message.content if block.type == "text").strip()

    async def _write_referenced(
        self, ticker: str, document: SourceDocument, seconds: float, context: dict[str, Any] | None
    ) -> Summary:
        context = {k: v for k, v in (context or {}).items() if k != "consensus_snapshot"} or None
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
        evidence: dict[str, Any] = {
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
            validation="source-references/1",
        )
