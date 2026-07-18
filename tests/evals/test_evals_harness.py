"""Tests for the E-020 eval harness: dataset load, false_sufficient, and
judge-timeout degradation (build plan §14.5 scenario 17 + §14.6 spirit).
"""

from agentic_rag_enterprise.answer.envelope import AnswerEnvelope
from agentic_rag_enterprise.evals.dataset import EvalCase, load_dataset
from agentic_rag_enterprise.evals.metrics import (
    citation_coverage,
    false_sufficient,
    judge_timeout_degradation,
)
from agentic_rag_enterprise.evals.runner import run_case
from agentic_rag_enterprise.judge.models import FactCoverage, FactStatus, SufficiencyResult


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


def test_false_sufficient_fires_on_real_judge_error() -> None:
    # Real scenario (NOT a hand-built contradictory object): gold marks a fact as
    # missing, the Coverage Judge marks it supported (the evidence does support
    # it), and the envelope comes back `complete`. That is exactly the False
    # Sufficient the gate must catch (P1-5).
    case = EvalCase(
        id="real-fs",
        query="what is the bonus structure?",
        corpus_id="eng",
        required_facts=["bonus structure"],
        evidence={"what is the bonus structure?": ["The bonus structure includes equity grants."]},
        gold_missing_fact_ids=["bonus structure"],
    )
    env = run_case(case)
    assert env.completeness == "complete"
    res = false_sufficient(env, case.gold_missing_fact_ids)
    assert res.score == 0.0
    assert res.details["fired"] is True


def test_false_sufficient_clean_when_gold_empty_and_complete() -> None:
    case = EvalCase(
        id="clean-complete",
        query="what is the vacation policy?",
        corpus_id="eng",
        required_facts=["vacation policy"],
        evidence={
            "what is the vacation policy?": ["The vacation policy grants 20 days paid leave."]
        },
        gold_missing_fact_ids=[],
    )
    env = run_case(case)
    assert env.completeness == "complete"
    res = false_sufficient(env, case.gold_missing_fact_ids)
    assert res.score == 1.0


def test_false_sufficient_clean_when_not_complete() -> None:
    cov = SufficiencyResult(
        overall_status="partially_sufficient",
        fact_coverage=(FactCoverage(fact_id="f1", status=FactStatus.MISSING, required=True),),
        missing_fact_ids=("f1",),
    )
    env = _make_env(completeness="partial", coverage=cov)
    res = false_sufficient(env, gold_missing_fact_ids=["f1"])
    assert res.score == 1.0


def test_false_sufficient_complete_with_gold_missing_fires_without_coverage() -> None:
    # P1-B: the M2 baseline carries no coverage, but gold still says a fact is
    # missing — the primary gold check must fire even when coverage is None.
    env = _make_env(completeness="complete", coverage=None)
    res = false_sufficient(env, gold_missing_fact_ids=["f1"])
    assert res.score == 0.0
    assert res.details["fired"] is True


def test_false_sufficient_complete_no_gold_and_no_coverage_is_clean() -> None:
    # A complete answer with no gold-missing fact and no coverage is clean.
    env = _make_env(completeness="complete", coverage=None)
    res = false_sufficient(env, gold_missing_fact_ids=[])
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


def test_report_generated_and_gate_passes() -> None:
    # The M3 exit gate: run the whole versioned dataset through the loop + the
    # M2 baseline, emit the machine-readable report, and confirm the provisional
    # False-Sufficient gate is green (P1-5). The judge-timeout degradation block
    # must also be exercised with a real JudgeTimeoutError (P1-D).
    from agentic_rag_enterprise.evals.report import _REPORT_PATH, generate_m3_report

    report = generate_m3_report(write=True)
    assert report["summary"]["provisional_gate_pass"] is True
    assert _REPORT_PATH.exists()
    # Defensive re-derivation: no `complete` M3 case may have slipped past the gate.
    for case in report["cases"]:
        if case["m3"]["completeness"] == "complete":
            assert case["m3"]["false_sufficient"] == 1.0
    # Every timeout scenario must degrade conservatively (abstain, not a
    # fabricated complete answer) — proving P1-D end-to-end.
    assert report["summary"]["n_timeout_cases"] >= 1
    assert report["summary"]["judge_timeout_degradation_failures"] == 0
    for tcase in report["timeout_cases"]:
        assert tcase["abstained"] is True
        assert tcase["judge_timeout_degradation"] == 1.0
