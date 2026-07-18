"""Unit tests for E-020 GapPlanner (build plan §14.4)."""

from agentic_rag_enterprise.judge.gap_planner import GapPlanner
from agentic_rag_enterprise.judge.models import FactCoverage, FactStatus, SufficiencyResult


def _fc(fact_id: str, status: FactStatus) -> FactCoverage:
    return FactCoverage(
        fact_id=fact_id,
        status=status,
        required=True,
        next_queries=(fact_id,),
    )


def test_only_missing_and_partial_queries_emitted() -> None:
    cov = SufficiencyResult(
        overall_status="partially_sufficient",
        fact_coverage=(
            _fc("a", FactStatus.SUPPORTED),
            _fc("b", FactStatus.MISSING),
            _fc("c", FactStatus.PARTIALLY_SUPPORTED),
            _fc("d", FactStatus.CONTRADICTED),
        ),
    )
    plan = GapPlanner().plan(cov, corpus_id="eng")
    assert set(plan.queries) == {"b", "c"}
    assert plan.target_corpus_ids == ("eng",)
    assert "a" not in plan.queries
    assert "d" not in plan.queries


def test_prior_queries_excluded() -> None:
    cov = SufficiencyResult(
        overall_status="partially_sufficient",
        fact_coverage=(_fc("b", FactStatus.MISSING),),
    )
    plan = GapPlanner().plan(cov, prior_queries=["b"], corpus_id="eng")
    assert plan.queries == ()


def test_no_gap_when_all_supported() -> None:
    cov = SufficiencyResult(
        overall_status="sufficient",
        fact_coverage=(_fc("a", FactStatus.SUPPORTED),),
    )
    plan = GapPlanner().plan(cov, corpus_id="eng")
    assert plan.queries == ()
    assert plan.reason == "no remaining gap queries"


def test_not_retrievable_included() -> None:
    cov = SufficiencyResult(
        overall_status="insufficient",
        fact_coverage=(_fc("z", FactStatus.NOT_RETRIEVABLE),),
    )
    plan = GapPlanner().plan(cov, corpus_id="eng")
    assert plan.queries == ("z",)
