"""Tests for the E-020 eval harness: dataset load, false_sufficient, and
judge-timeout degradation (build plan §14.5 scenario 17 + §14.6 spirit).
"""

from agentic_rag_enterprise.answer.envelope import AnswerEnvelope
from agentic_rag_enterprise.evals.dataset import load_dataset
from agentic_rag_enterprise.evals.metrics import (
    citation_coverage,
    false_sufficient,
    judge_timeout_degradation,
)
from agentic_rag_enterprise.judge.models import FactCoverage, FactStatus, SufficiencyResult
from agentic_rag_enterprise.judge.query_fact_extractor import make_required_fact


def _make_env(
    *, completeness: str, coverage: SufficiencyResult | None, abstained: bool = False
) -> AnswerEnvelope:
    confidence = "high" if completeness == "complete" else "low"
    return AnswerEnvelope(
        request_id="r",
        session_id="s",
        answer_markdown="x",
        claims=(),
        evidence=(),
        citations=(),
        completeness=completeness,  # type: ignore[arg-type]
        confidence=confidence,  # type: ignore[arg-type]
        corpora_used=("eng",),
        coverage=coverage,
        stop_reason="no_evidence" if abstained else "evidence_found",
        abstained=abstained,
    )


def test_dataset_loads() -> None:
    ds = load_dataset("m3_v1")
    assert ds.version == "m3_v1"
    assert len(ds.cases) >= 5


def test_false_sufficient_fires_on_complete_with_missing_fact() -> None:
    fid = make_required_fact("bonus structure").fact_id
    cov = SufficiencyResult(
        overall_status="sufficient",
        fact_coverage=(
            FactCoverage(fact_id=fid, status=FactStatus.MISSING, required=True),
            FactCoverage(fact_id="f2", status=FactStatus.SUPPORTED, required=True),
        ),
        missing_fact_ids=(fid,),
    )
    env = _make_env(completeness="complete", coverage=cov)
    res = false_sufficient(env, gold_missing_fact_ids=[fid])
    assert res.score == 0.0
    assert res.details["fired"] is True


def test_false_sufficient_clean_when_not_complete() -> None:
    cov = SufficiencyResult(
        overall_status="partially_sufficient",
        fact_coverage=(FactCoverage(fact_id="f1", status=FactStatus.MISSING, required=True),),
        missing_fact_ids=("f1",),
    )
    env = _make_env(completeness="partial", coverage=cov)
    res = false_sufficient(env, gold_missing_fact_ids=["f1"])
    assert res.score == 1.0


def test_false_sufficient_no_coverage_is_clean() -> None:
    env = _make_env(completeness="complete", coverage=None)
    res = false_sufficient(env, gold_missing_fact_ids=["f1"])
    assert res.score == 1.0


def test_judge_timeout_degradation_flags_fabricated_complete() -> None:
    env = _make_env(completeness="complete", coverage=None)
    res = judge_timeout_degradation(env)
    assert res.score == 0.0  # a confident complete answer after a judge fault is a failure


def test_judge_timeout_degradation_accepts_abstain() -> None:
    env = _make_env(completeness="insufficient", coverage=None, abstained=True)
    res = judge_timeout_degradation(env)
    assert res.score == 1.0


def test_judge_timeout_degradation_accepts_partial() -> None:
    env = _make_env(completeness="partial", coverage=None)
    res = judge_timeout_degradation(env)
    assert res.score == 1.0


def test_citation_coverage_baseline() -> None:
    res = citation_coverage(["e1", "e2"], ["e1"])
    assert res.score == 1.0
    res2 = citation_coverage(["e1"], ["e1", "e2"])
    assert res2.score == 0.5
