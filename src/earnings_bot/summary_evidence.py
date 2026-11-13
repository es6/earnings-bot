"""Unverified structured candidates for item 6; never a live-renderer input.

Schema and reference integrity do not prove metric, period, basis or value binding.
The semantic verifier must establish those before these candidates can be published.
"""

import re
from decimal import Decimal
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from earnings_bot.source_document import SourceBlock, SourceDocument, SourceTable


class _Candidate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class EvidenceRef(_Candidate):
    document_sha256: str
    block_id: str
    cell_ids: tuple[str, ...]  # empty for paragraphs; original cells for a table
    quote: str = Field(min_length=1)


class SourcedValue(_Candidate):
    amount: str  # decimal string, never a float or a formatted "$10B"
    upper: str | None  # inclusive range endpoint, absent for a scalar
    unit: str = Field(min_length=1)  # money, per share, vehicles, square feet, %, bps, ...
    scale: Literal["one", "thousand", "million", "billion", "trillion"]
    currency: str  # empty for nonmonetary units
    value_qualifier: Literal[
        "exact", "approximately", "nearly", "more_than", "at_least", "less_than", "at_most"
    ]
    period: str  # source fiscal label; unknown metadata stays empty
    duration: str
    basis: str
    comparison: Literal["", "YoY", "QoQ", "versus prior guidance"]
    comparison_qualifier: str  # exact exclusions, e.g. constant currency/ex-acquisition
    evidence: tuple[EvidenceRef, ...] = Field(min_length=1)

    @field_validator("amount", "upper")
    @classmethod
    def decimal_string(cls, value: str | None) -> str | None:
        if value is not None and re.fullmatch(r"[+-]?\d+(?:\.\d+)?", value, re.ASCII) is None:
            raise ValueError("Use a finite base-10 decimal string without units or separators")
        return value

    @model_validator(mode="after")
    def ordered_range(self) -> Self:
        if self.upper is not None and Decimal(self.upper) < Decimal(self.amount):
            raise ValueError("Range upper endpoint is below its lower endpoint")
        if not self.unit.strip():
            raise ValueError("A value needs its unit")
        return self


class ReferencedFact(_Candidate):
    metric: str = Field(min_length=1)
    kind: Literal["actual", "segment", "guide", "update"]
    role: Literal["headline", "driver", "guidance", "material_context", "update", "supporting"]
    group: str
    relevance: str
    current_actual: SourcedValue | None
    prior_year_actual: SourcedValue | None
    reported_growth: SourcedValue | None
    comparable_growth: SourcedValue | None
    previous_guidance: SourcedValue | None
    new_guidance: SourcedValue | None
    update: str
    update_evidence: tuple[EvidenceRef, ...]

    @model_validator(mode="after")
    def has_content(self) -> Self:
        if bool(self.update.strip()) != bool(self.update_evidence):
            raise ValueError("A business update and its evidence must be supplied together")
        if not self.metric.strip() or not (any(self.values()) or self.update.strip()):
            raise ValueError("A fact needs a metric and at least one value or sourced update")
        return self

    def values(self) -> tuple[SourcedValue | None, ...]:
        return tuple(getattr(self, field) for field in VALUE_FIELDS)


VALUE_FIELDS = (
    "current_actual",
    "prior_year_actual",
    "reported_growth",
    "comparable_growth",
    "previous_guidance",
    "new_guidance",
)


class UnverifiedBrief(_Candidate):
    schema_version: Literal["source-values/1"]
    facts: tuple[ReferencedFact, ...]


def _reference_issues(reference: EvidenceRef, document: SourceDocument) -> list[str]:
    try:
        block = document.resolve(reference.document_sha256, reference.block_id)
        if not isinstance(block, (SourceBlock, SourceTable)):
            return ["block_id must name a paragraph or table"]
        if isinstance(block, SourceTable):
            if block.nested:
                return ["a nested outer layout table cannot establish independent bindings"]
            if not reference.cell_ids:
                return ["table evidence needs cell references"]
            cells = {cell.id for cell in block.cells}
            if any(cell_id not in cells for cell_id in reference.cell_ids):
                return ["a cited cell does not belong to this table"]
        elif reference.cell_ids:
            return ["paragraph evidence cannot cite table cells"]
        if not reference.quote.strip() or " ".join(reference.quote.split()) not in " ".join(
            block.text.split()
        ):
            return ["quote is absent from the cited block"]
    except ValueError as error:
        return [str(error)]
    return []


def parse_referenced_brief(answer: str, document: SourceDocument) -> UnverifiedBrief:
    """Reject malformed or unresolvable evidence; return an UNVERIFIED candidate.

    An existing cell and a local quote do not establish the claim's meaning. No
    conversion to SummaryFact or live rendering is provided here.
    """
    candidate = UnverifiedBrief.model_validate_json(answer)
    issues: list[str] = []
    for index, fact in enumerate(candidate.facts):
        references = [("update", fact.update_evidence)]
        references.extend(
            (field, value.evidence)
            for field, value in zip(VALUE_FIELDS, fact.values(), strict=True)
            if value is not None
        )
        for field, citations in references:
            for reference in citations:
                issues.extend(
                    f"fact {index + 1}.{field}: {issue}"
                    for issue in _reference_issues(reference, document)
                )
    if issues:
        raise ValueError("Unresolvable evidence: " + "; ".join(issues))
    return candidate
