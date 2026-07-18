"""Unit tests for E-020 StopPolicy (build plan §14.5)."""

from agentic_rag_enterprise.judge.stop_policy import StopPolicy


def test_stop_on_sufficient() -> None:
    d = StopPolicy().decide(
        round=0,
        max_rounds=3,
        overall_status="sufficient",
        can_continue=True,
        new_evidence_ids={"e1"},
        new_covered_fact_ids=set(),
    )
    assert d.should_stop and d.reason == "sufficient"


def test_stop_on_max_rounds() -> None:
    d = StopPolicy().decide(
        round=2,
        max_rounds=3,
        overall_status="insufficient",
        can_continue=True,
        new_evidence_ids={"e1"},
        new_covered_fact_ids=set(),
    )
    assert d.should_stop and d.reason == "max_rounds"


def test_stop_on_no_new_evidence() -> None:
    # Two consecutive no-gain rounds => no_new_evidence (round 1 adds nothing new).
    d = StopPolicy().decide(
        round=1,
        max_rounds=3,
        overall_status="partially_sufficient",
        can_continue=True,
        new_evidence_ids=set(),
        new_covered_fact_ids=set(),
    )
    assert d.should_stop and d.reason == "no_new_evidence"


def test_continue_when_new_evidence_present() -> None:
    d = StopPolicy().decide(
        round=1,
        max_rounds=3,
        overall_status="partially_sufficient",
        can_continue=True,
        new_evidence_ids={"e2"},
        new_covered_fact_ids=set(),
    )
    assert not d.should_stop and d.reason == "continue"


def test_stop_on_budget_exhausted() -> None:
    d = StopPolicy().decide(
        round=0,
        max_rounds=3,
        overall_status="insufficient",
        can_continue=True,
        new_evidence_ids={"e1"},
        new_covered_fact_ids=set(),
        budget_remaining=0.0,
    )
    assert d.reason == "budget_exhausted"


def test_stop_on_judge_failure() -> None:
    d = StopPolicy().decide(
        round=0,
        max_rounds=3,
        overall_status="insufficient",
        can_continue=True,
        new_evidence_ids={"e1"},
        new_covered_fact_ids=set(),
        judge_ok=False,
    )
    assert d.reason == "tool_unavailable"


def test_stop_on_all_sources_exhausted() -> None:
    d = StopPolicy().decide(
        round=0,
        max_rounds=3,
        overall_status="insufficient",
        can_continue=False,
        new_evidence_ids={"e1"},
        new_covered_fact_ids=set(),
    )
    assert d.reason == "all_sources_exhausted"


def test_continue_when_new_covered_fact() -> None:
    d = StopPolicy().decide(
        round=1,
        max_rounds=3,
        overall_status="partially_sufficient",
        can_continue=True,
        new_evidence_ids=set(),
        new_covered_fact_ids={"a"},
    )
    assert not d.should_stop and d.reason == "continue"
