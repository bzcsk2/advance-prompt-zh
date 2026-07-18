"""E-019/E-020 Required-Fact Coverage + iteration models (build plan §7.7 / §7.8 / §14.4 / §14.5).

These are the *quality-iteration* models introduced in Milestone 3. They are
deliberately independent of the answer-layer ``AnswerEnvelope`` / ``Claim`` types
so the judge package has no import-time dependency on ``answer`` (the judge is
imported by ``answer/envelope.py`` for the optional ``coverage`` field, and the
judge's components import ``answer.verification`` — a one-directional edge from
``judge`` → ``answer`` is fine; the reverse edge only touches ``judge.models``,
which imports nothing from ``answer``).

All result models are frozen + validated so their fields can never contradict
one another (mirrors the E-012 / E-013 validated-model approach).
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# Overall verdicts the Coverage Judge can emit (build plan §14.3). Note
# `not_retrievable` is a *fact* status only and is never an overall status.
OverallStatus = Literal[
    "sufficient",
    "partially_sufficient",
    "insufficient",
    "contradicted",
    "ambiguous",
    "policy_blocked",
]

StopReason = Literal[
    "sufficient",
    "budget_exhausted",
    "no_new_evidence",
    "max_rounds",
    "duplicate_evidence",
    "all_sources_exhausted",
    "low_retrieval_quality",
    "policy_blocked",
    "tool_unavailable",
    "user_clarification_required",
    "contradicted",
    "continue",  # not a terminal stop — the loop should keep iterating
]


class FactStatus(str, Enum):
    """Per-Required-Fact coverage status (build plan §14.3)."""

    SUPPORTED = "supported"
    PARTIALLY_SUPPORTED = "partially_supported"
    MISSING = "missing"
    CONTRADICTED = "contradicted"
    AMBIGUOUS = "ambiguous"
    POLICY_BLOCKED = "policy_blocked"
    NOT_RETRIEVABLE = "not_retrievable"


class RequiredFact(BaseModel):
    """A fact the answer is expected to establish (build plan §7.7)."""

    model_config = ConfigDict(frozen=True)

    fact_id: str
    description: str
    required: bool = True
    depends_on_fact_ids: tuple[str, ...] = Field(default_factory=tuple)


class FactCoverage(BaseModel):
    """Coverage verdict for a single Required Fact (build plan §7.7)."""

    model_config = ConfigDict(frozen=True)

    fact_id: str
    status: FactStatus
    required: bool = True
    evidence_ids: tuple[str, ...] = Field(default_factory=tuple)
    explanation: str = ""
    missing_information: str | None = None
    next_queries: tuple[str, ...] = Field(default_factory=tuple)
    target_corpus_ids: tuple[str, ...] = Field(default_factory=tuple)


class SufficiencyResult(BaseModel):
    """Coverage Judge output — conforms to the SufficiencyResult schema (§7.8 / §14.2)."""

    model_config = ConfigDict(frozen=True)

    overall_status: OverallStatus
    fact_coverage: tuple[FactCoverage, ...] = Field(default_factory=tuple)

    covered_fact_ids: tuple[str, ...] = Field(default_factory=tuple)
    missing_fact_ids: tuple[str, ...] = Field(default_factory=tuple)
    contradicted_fact_ids: tuple[str, ...] = Field(default_factory=tuple)

    can_continue_retrieval: bool = False
    should_ask_clarification: bool = False
    should_abstain: bool = False

    next_queries: tuple[str, ...] = Field(default_factory=tuple)
    target_corpus_ids: tuple[str, ...] = Field(default_factory=tuple)

    confidence: float = Field(default=1.0, ge=0.0, le=1.0)


# The Coverage Judge output conforms to SufficiencyResult (§14.2): the two names
# are kept distinct for readability at call sites, but they are the same type.
CoverageJudgeResult = SufficiencyResult


class GapRetrievalPlan(BaseModel):
    """Next-round retrieval plan from the Gap Planner (build plan §14.4)."""

    model_config = ConfigDict(frozen=True)

    queries: tuple[str, ...] = Field(default_factory=tuple)
    target_corpus_ids: tuple[str, ...] = Field(default_factory=tuple)
    fact_ids: tuple[str, ...] = Field(default_factory=tuple)
    reason: str = ""


class StopDecision(BaseModel):
    """Iteration stop decision (build plan §14.5)."""

    model_config = ConfigDict(frozen=True)

    should_stop: bool
    reason: StopReason
    explanation: str = ""


def derive_overall_status(
    required_coverages: list[FactCoverage],
) -> OverallStatus:
    """Compute the overall status from the *required* fact coverages.

    Fixed priority (build plan §14.3):
    ``policy_blocked > ambiguous > contradicted > sufficient > partially_sufficient > insufficient``.
    ``partially_sufficient`` requires at least one required fact to be ``supported``;
    with no supported fact at all the result collapses to ``insufficient``.
    ``not_retrievable`` is treated like ``missing`` for overall purposes (no evidence
    was retrievable for that fact).
    """
    if not required_coverages:
        return "sufficient"  # vacuously satisfied when there are no required facts

    statuses = [c.status for c in required_coverages]
    if FactStatus.POLICY_BLOCKED in statuses:
        return "policy_blocked"
    if FactStatus.AMBIGUOUS in statuses:
        return "ambiguous"
    if FactStatus.CONTRADICTED in statuses:
        return "contradicted"
    if all(s is FactStatus.SUPPORTED for s in statuses):
        return "sufficient"
    if any(s is FactStatus.SUPPORTED for s in statuses):
        return "partially_sufficient"
    return "insufficient"


def build_sufficiency_result(
    *,
    coverages: list[FactCoverage],
    required_fact_ids: set[str] | None = None,
) -> SufficiencyResult:
    """Assemble a :class:`SufficiencyResult` from per-fact coverages.

    `required_fact_ids` restricts the overall-status derivation to required facts
    (optional missing facts must not force an ``insufficient`` verdict, §14.3).
    """
    required = (
        [c for c in coverages if c.required]
        if required_fact_ids is None
        else [c for c in coverages if c.fact_id in required_fact_ids]
    )
    overall = derive_overall_status(required if required else coverages)

    covered = tuple(c.fact_id for c in coverages if c.status is FactStatus.SUPPORTED)
    missing = tuple(
        c.fact_id for c in coverages if c.status in (FactStatus.MISSING, FactStatus.NOT_RETRIEVABLE)
    )
    contradicted = tuple(c.fact_id for c in coverages if c.status is FactStatus.CONTRADICTED)

    can_continue = overall in ("partially_sufficient", "insufficient") and (
        any(c.status in (FactStatus.MISSING, FactStatus.PARTIALLY_SUPPORTED) for c in coverages)
    )
    return SufficiencyResult(
        overall_status=overall,
        fact_coverage=tuple(coverages),
        covered_fact_ids=covered,
        missing_fact_ids=missing,
        contradicted_fact_ids=contradicted,
        can_continue_retrieval=can_continue,
        should_ask_clarification=overall == "ambiguous",
        should_abstain=overall in ("insufficient",),
        next_queries=tuple(q for c in coverages for q in c.next_queries),
        target_corpus_ids=tuple(cid for c in coverages for cid in c.target_corpus_ids),
        confidence=1.0 if overall == "sufficient" else 0.5,
    )
