"""E-020 eval metrics (build plan §14 / M3).

Metric functions take a produced :class:`AnswerEnvelope` (and, where relevant,
gold labels) and return an :class:`EvalResult` with a ``score`` in ``[0, 1]``
(1.0 = good) plus machine-readable ``details``. These are the offline guards that
catch the two failure modes the M3 iteration loop is specifically meant to
prevent: falsely reporting ``complete`` when a required fact is uncovered, and
presenting a confident fabricated answer when the judge itself fails.
"""

from agentic_rag_enterprise.answer.envelope import AnswerEnvelope
from pydantic import BaseModel


class EvalResult(BaseModel):
    name: str
    score: float
    details: dict = {}


def citation_coverage(answer_citations: list[str], required_evidence_ids: list[str]) -> EvalResult:
    """Measure whether required evidence ids appear in the answer citation map."""
    if not required_evidence_ids:
        return EvalResult(
            name="citation_coverage", score=1.0, details={"reason": "no required ids"}
        )

    covered = set(answer_citations) & set(required_evidence_ids)
    score = len(covered) / len(set(required_evidence_ids))
    return EvalResult(
        name="citation_coverage",
        score=score,
        details={"covered": sorted(covered), "required": required_evidence_ids},
    )


def false_sufficient(
    envelope: AnswerEnvelope,
    gold_missing_fact_ids: list[str],
) -> EvalResult:
    """Guard against a falsely ``complete`` answer that hides a missing fact.

    Fires (score 0.0) when the envelope reports ``completeness == "complete"``
    but, per its attached ``coverage``, at least one *required* fact that the
    gold answer expects to be missing is in fact ``missing`` / ``contradicted``.
    A correctly conservative system would never claim ``complete`` in that case.

    Args:
        envelope: The produced answer envelope (must carry ``coverage``).
        gold_missing_fact_ids: Fact ids the gold/standard answer expects to be
            uncovered for this query (i.e. the answer should NOT be ``complete``).
    """
    if envelope.coverage is None:
        return EvalResult(
            name="false_sufficient", score=1.0, details={"reason": "no coverage attached"}
        )
    if envelope.completeness != "complete":
        return EvalResult(
            name="false_sufficient",
            score=1.0,
            details={"reason": "not complete", "completeness": envelope.completeness},
        )

    uncovered = set(envelope.coverage.missing_fact_ids) | set(
        envelope.coverage.contradicted_fact_ids
    )
    gold = set(gold_missing_fact_ids)
    fired = bool(gold & uncovered)
    return EvalResult(
        name="false_sufficient",
        score=0.0 if fired else 1.0,
        details={
            "fired": fired,
            "uncovered": sorted(uncovered),
            "gold_missing": sorted(gold),
        },
    )


def judge_timeout_degradation(envelope: AnswerEnvelope) -> EvalResult:
    """Guard that a judge fault degrades conservatively, never a fabricated answer.

    When the Coverage Judge raises (e.g. ``JudgeTimeoutError``), the service must
    not return a confidently ``complete`` answer built on unverified coverage. A
    degraded answer is one whose ``completeness`` is anything other than
    ``"complete"`` (``partial`` / ``insufficient`` / ``conflicted``) or that has
    ``abstained is True``.

    Args:
        envelope: The produced answer envelope after a simulated judge fault.
    """
    degraded = envelope.abstained or envelope.completeness != "complete"
    return EvalResult(
        name="judge_timeout_degradation",
        score=1.0 if degraded else 0.0,
        details={
            "degraded": degraded,
            "completeness": envelope.completeness,
            "abstained": envelope.abstained,
        },
    )
