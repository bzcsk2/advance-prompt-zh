"""Unit tests for E-019 DeterministicCoverageJudge (build plan §14.1–§14.3)."""

from datetime import datetime

from agentic_rag_enterprise.domain.evidence import Evidence as SnapshotEvidence
from agentic_rag_enterprise.judge.deterministic_coverage_judge import (
    DeterministicCoverageJudge,
)
from agentic_rag_enterprise.judge.models import FactStatus, RequiredFact


def _ev(evidence_id: str, text: str, tenant_id: str = "t1") -> SnapshotEvidence:
    return SnapshotEvidence(
        evidence_id=evidence_id,
        tenant_id=tenant_id,
        corpus_id="eng",
        document_id="d1",
        document_version="v1",
        source_uri="inline://d1",
        source_filename="d1.md",
        text=text,
        text_hash="h",
        retrieval_query="q",
        authority_level=50,
        retrieved_at=datetime(2024, 1, 1),
        acl_policy_id="default",
        policy_version="1.0",
        retrieval_iteration=0,
    )


def _fact(desc: str) -> RequiredFact:
    return RequiredFact(fact_id=f"f_{desc}", description=desc)


def test_supported() -> None:
    judge = DeterministicCoverageJudge()
    res = judge.judge(
        query="q",
        required_facts=[_fact("vacation policy")],
        evidence=(_ev("e1", "The vacation policy grants 20 days paid leave per year."),),
    )
    assert res.overall_status == "sufficient"
    assert res.fact_coverage[0].status is FactStatus.SUPPORTED


def test_partially_supported() -> None:
    judge = DeterministicCoverageJudge()
    # 6 fact tokens, only 2 overlap the evidence (< 50%) -> partial.
    res = judge.judge(
        query="q",
        required_facts=[_fact("the quarterly revenue target for the emea region expansion")],
        evidence=(_ev("e1", "the quarterly revenue increased"),),
    )
    assert res.fact_coverage[0].status is FactStatus.PARTIALLY_SUPPORTED


def test_missing_when_no_overlap() -> None:
    judge = DeterministicCoverageJudge()
    res = judge.judge(
        query="q",
        required_facts=[_fact("secret project codename")],
        evidence=(_ev("e1", "the weather is sunny today"),),
    )
    assert res.fact_coverage[0].status is FactStatus.MISSING
    assert res.overall_status == "insufficient"


def test_not_retrievable_when_empty_evidence() -> None:
    judge = DeterministicCoverageJudge()
    res = judge.judge(
        query="q",
        required_facts=[_fact("secret project codename")],
        evidence=(),
    )
    assert res.fact_coverage[0].status is FactStatus.NOT_RETRIEVABLE


def test_contradicted_with_negation() -> None:
    judge = DeterministicCoverageJudge()
    res = judge.judge(
        query="q",
        required_facts=[_fact("office in new york")],
        evidence=(_ev("e1", "The office is not in new york; it is in boston."),),
    )
    assert res.fact_coverage[0].status is FactStatus.CONTRADICTED
    assert res.overall_status == "contradicted"


def test_overall_priority_contradicted_beats_supported() -> None:
    judge = DeterministicCoverageJudge()
    res = judge.judge(
        query="q",
        required_facts=[
            _fact("vacation policy"),
            _fact("office in new york"),
        ],
        evidence=(
            _ev("e1", "The vacation policy grants 20 days paid leave per year."),
            _ev("e2", "The office is not in new york; it is in boston."),
        ),
    )
    assert res.overall_status == "contradicted"


def test_next_queries_emitted_for_missing() -> None:
    judge = DeterministicCoverageJudge()
    res = judge.judge(
        query="q",
        required_facts=[_fact("secret project codename")],
        evidence=(_ev("e1", "the weather is sunny"),),
    )
    assert res.fact_coverage[0].next_queries == ("secret project codename",)
    assert res.next_queries == ("secret project codename",)
