"""Unit tests for the E-012 single-corpus Fast Path (build plan §5.2 / §14.3).

Hermetic: a fake ``SecureRetriever`` records the single ``retrieve_evidence``
call and returns controlled ``Evidence`` lists, so no Qdrant, parent store, or
encoder is touched. The tests assert the one-pass contract: exactly one
retrieval, the ``SecurityContext`` and ``CorpusConfig`` reach the secure
boundary unchanged, no Planner / second round is produced, and a retrieval
fault propagates as a typed ``FastPathBackendError`` rather than as
``insufficient``.
"""

from datetime import datetime

import pytest

from agentic_rag_enterprise.domain.corpus import CorpusConfig
from agentic_rag_enterprise.domain.evidence import Evidence as SnapshotEvidence
from agentic_rag_enterprise.domain.security import SecurityContext
from agentic_rag_enterprise.retrieval.fast_path import (
    FastPathBackendError,
    FastPathResult,
    FastPathStopReason,
    FastPathSufficiency,
    run_fast_path,
)
from agentic_rag_enterprise.storage.vector_store import DenseEncoder, SparseEncoder
from tests.fixtures import FakeDenseEncoder, FakeSparseEncoder, make_security_context


def _corpus(corpus_id: str = "eng", tenant_id: str = "t1") -> CorpusConfig:
    return CorpusConfig(
        corpus_id=corpus_id,
        tenant_id=tenant_id,
        name="Eng",
        description="",
        domain="",
        owner="",
        source_type="wiki",
        capability_ids=[],
        enabled=True,
        searchable=True,
        security_policy_id="p",
        default_security_level="internal",
        created_at=datetime.now(),
        updated_at=datetime.now(),
    )


def _evidence(evidence_id: str = "e1") -> SnapshotEvidence:
    return SnapshotEvidence(
        evidence_id=evidence_id,
        tenant_id="t1",
        corpus_id="eng",
        document_id="doc1",
        document_version="v1",
        source_uri="inline://doc1",
        source_filename="doc1.md",
        text="some grounded body",
        text_hash="h",
        retrieval_query="q",
        authority_level=50,
        retrieved_at=datetime.now(),
        acl_policy_id="default",
        policy_version="1.0",
        retrieval_iteration=0,
    )


class _FakeSecureRetriever:
    """Records the single retrieve_evidence call; returns a canned list or raises."""

    def __init__(self, payload: object) -> None:
        # payload is either a list[Evidence] or an Exception instance to raise.
        self._payload = payload
        self.calls: list[tuple] = []

    def retrieve_evidence(
        self,
        ctx: SecurityContext,
        query: str,
        corpus: CorpusConfig,
        top_k: object = None,
        *,
        dense_encoder: DenseEncoder,
        sparse_encoder: SparseEncoder,
        iteration: int = 0,
        plan_step_id: object = None,
    ) -> list[SnapshotEvidence]:
        self.calls.append((ctx, query, corpus, top_k))
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload  # type: ignore[return-value]


def _run(retriever: _FakeSecureRetriever, query: str = "q", corpus: CorpusConfig | None = None):
    corpus = corpus or _corpus()
    return run_fast_path(
        retriever,
        make_security_context(),
        query,
        corpus,
        dense_encoder=FakeDenseEncoder(),
        sparse_encoder=FakeSparseEncoder(),
    )


def test_sufficient_when_evidence_present() -> None:
    retriever = _FakeSecureRetriever([_evidence()])
    result = _run(retriever)

    assert isinstance(result, FastPathResult)
    assert result.sufficiency is FastPathSufficiency.SUFFICIENT
    assert result.stop_reason is FastPathStopReason.EVIDENCE_FOUND
    assert result.is_sufficient is True
    assert result.should_abstain is False
    assert len(result.evidence) == 1
    assert result.evidence[0].evidence_id == "e1"


def test_insufficient_when_no_evidence() -> None:
    retriever = _FakeSecureRetriever([])
    result = _run(retriever)

    assert result.sufficiency is FastPathSufficiency.INSUFFICIENT
    assert result.stop_reason is FastPathStopReason.NO_EVIDENCE
    assert result.is_sufficient is False
    assert result.should_abstain is True
    assert result.evidence == ()


def test_exactly_one_retrieve_evidence_call() -> None:
    retriever = _FakeSecureRetriever([_evidence()])
    _run(retriever)
    assert len(retriever.calls) == 1


def test_context_and_corpus_passed_unchanged() -> None:
    ctx = make_security_context()
    corpus = _corpus()
    retriever = _FakeSecureRetriever([_evidence()])
    run_fast_path(
        retriever,
        ctx,
        "my query",
        corpus,
        dense_encoder=FakeDenseEncoder(),
        sparse_encoder=FakeSparseEncoder(),
    )

    recorded_ctx, recorded_query, recorded_corpus, _top_k = retriever.calls[0]
    # Identity, not a copy: the same objects enter the secure retrieval boundary.
    assert recorded_ctx is ctx
    assert recorded_corpus is corpus
    assert recorded_query == "my query"


def test_no_planner_no_second_round() -> None:
    retriever = _FakeSecureRetriever([_evidence()])
    _run(retriever)
    # Only one retrieval pass; assert no further calls were made.
    assert len(retriever.calls) == 1


def test_retrieval_error_propagates_as_backend_error() -> None:
    retriever = _FakeSecureRetriever(RuntimeError("qdrant down"))

    try:
        _run(retriever)
    except FastPathBackendError as exc:
        assert isinstance(exc.__cause__, RuntimeError)
        assert "qdrant down" in str(exc.__cause__)
    else:
        raise AssertionError("expected FastPathBackendError, got a result")


def test_contradictory_state_empty_evidence_but_sufficient_rejected() -> None:
    # External direct construction must be rejected: empty evidence cannot be
    # "sufficient" (build plan §14.7). The frozen, validated model locks this.
    with pytest.raises(ValueError):
        FastPathResult(
            query="q",
            corpus_id="eng",
            tenant_id="t1",
            evidence=(),
            sufficiency=FastPathSufficiency.SUFFICIENT,
            stop_reason=FastPathStopReason.EVIDENCE_FOUND,
        )


def test_contradictory_state_evidence_but_insufficient_rejected() -> None:
    with pytest.raises(ValueError):
        FastPathResult(
            query="q",
            corpus_id="eng",
            tenant_id="t1",
            evidence=(_evidence(),),
            sufficiency=FastPathSufficiency.INSUFFICIENT,
            stop_reason=FastPathStopReason.NO_EVIDENCE,
        )


def test_contradictory_state_stop_reason_mismatch_rejected() -> None:
    # stop_reason must agree with sufficiency.
    with pytest.raises(ValueError):
        FastPathResult(
            query="q",
            corpus_id="eng",
            tenant_id="t1",
            evidence=(),
            sufficiency=FastPathSufficiency.INSUFFICIENT,
            stop_reason=FastPathStopReason.EVIDENCE_FOUND,
        )


def test_result_is_frozen_and_derived_fields_consistent() -> None:
    result = _run(_FakeSecureRetriever([_evidence()]))
    # Derived booleans are computed from sufficiency, not independently settable.
    assert result.is_sufficient is True
    assert result.should_abstain is False
    # Frozen model: mutating any field raises.
    with pytest.raises(Exception):
        result.sufficiency = FastPathSufficiency.INSUFFICIENT  # type: ignore[misc]
