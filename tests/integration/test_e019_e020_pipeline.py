"""Integration tests for the E-019/E-020 pipeline end-to-end.

Drives ``ChatService.answer_with_iteration`` through the deterministic
``DeterministicCoverageJudge`` + ``GapPlanner`` + ``StopPolicy`` using the
versioned eval dataset (offline, no LLM, no network). Asserts the loop reaches a
``sufficient`` verdict via gap retrieval and that every produced envelope is a
valid, locked :class:`AnswerEnvelope`.
"""

from agentic_rag_enterprise.evals.dataset import load_dataset
from agentic_rag_enterprise.evals.runner import run_case


def _case(case_id: str):
    return next(c for c in load_dataset("m3_v1").cases if c.id == case_id)


def test_dataset_loads() -> None:
    ds = load_dataset("m3_v1")
    assert ds.version == "m3_v1"
    assert len(ds.cases) >= 5


def test_sufficient_case_end_to_end() -> None:
    env = run_case(_case("case-sufficient"))
    assert env.coverage is not None
    assert env.coverage.overall_status == "sufficient"
    assert env.completeness == "complete"
    assert env.gap_rounds == 1


def test_gap_retrieval_reaches_sufficient_with_loop() -> None:
    env = run_case(_case("case-gap-retrieval"), max_rounds=3)
    assert env.coverage.overall_status == "sufficient"
    assert env.gap_rounds > 1
    assert len(env.evidence) == 2


def test_insufficient_case_abstains() -> None:
    env = run_case(_case("case-insufficient"))
    assert env.abstained is True
    assert env.stop_reason == "no_evidence"


def test_contradicted_case_is_conflicted_not_abstained() -> None:
    env = run_case(_case("case-contradicted"))
    assert env.coverage.overall_status == "contradicted"
    assert env.completeness == "conflicted"
    assert env.abstained is False


def test_partial_exhausted_case() -> None:
    env = run_case(_case("case-partial-exhausted"))
    assert env.coverage.overall_status == "partially_sufficient"
    assert env.completeness == "partial"
    assert env.gap_rounds > 1


def test_every_envelope_passes_lock_state() -> None:
    # The AnswerEnvelope validator runs on construction; every produced envelope
    # must be internally consistent (abstain <=> completeness insufficient).
    for case in load_dataset("m3_v1").cases:
        env = run_case(case)
        assert env.abstained == (env.completeness == "insufficient")
