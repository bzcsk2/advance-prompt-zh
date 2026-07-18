"""Unit tests for E-016 cross-corpus retrieval + merge/dedup (retrieval/multi_corpus.py)."""

from __future__ import annotations

from datetime import datetime

from agentic_rag_enterprise.domain.corpus import CorpusConfig
from agentic_rag_enterprise.domain.evidence import Evidence as SnapshotEvidence
from agentic_rag_enterprise.domain.security import SecurityContext
from agentic_rag_enterprise.retrieval.multi_corpus import (
    MultiCorpusRetrieval,
    merge_evidence,
)
from agentic_rag_enterprise.storage.vector_store import DenseEncoder, SparseEncoder


def _corpus(corpus_id: str, tenant_id: str = "local", authority_level: int = 50) -> CorpusConfig:
    return CorpusConfig(
        corpus_id=corpus_id,
        tenant_id=tenant_id,
        name=corpus_id,
        description="",
        domain="",
        owner="",
        source_type="documents",
        capability_ids=[],
        enabled=True,
        searchable=True,
        authority_level=authority_level,
        security_policy_id="p",
        default_security_level="internal",
        created_at=datetime(2024, 1, 1),
        updated_at=datetime(2024, 1, 1),
    )


def _evidence(
    evidence_id: str,
    text: str,
    *,
    tenant_id: str = "local",
    corpus_id: str,
    document_id: str = "d1",
    document_version: str = "v1",
    text_hash: str | None = None,
    authority_level: int = 50,
) -> SnapshotEvidence:
    return SnapshotEvidence(
        evidence_id=evidence_id,
        tenant_id=tenant_id,
        corpus_id=corpus_id,
        document_id=document_id,
        document_version=document_version,
        source_uri="inline://d1",
        source_filename="d1.md",
        text=text,
        text_hash=text_hash if text_hash is not None else f"h-{evidence_id}",
        retrieval_query="q",
        authority_level=authority_level,
        retrieved_at=datetime(2024, 1, 1),
        acl_policy_id="default",
        policy_version="1.0",
        retrieval_iteration=0,
    )


def _ctx(tenant_id: str = "local") -> SecurityContext:
    return SecurityContext(
        request_id="r",
        session_id="s",
        tenant_id=tenant_id,
        user_id="u",
        policy_version="1.0",
    )


class _FakeRetriever:
    """Query-independent fake: returns the per-corpus Evidence it was seeded with."""

    def __init__(self, per_corpus: dict[str, list[SnapshotEvidence]]) -> None:
        self._per_corpus = per_corpus

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
        return list(self._per_corpus.get(corpus.corpus_id, []))


class _FaultyRetriever:
    """Fake that raises for a configured set of corpus ids."""

    def __init__(self, raise_for: set[str], ok: dict[str, list[SnapshotEvidence]]) -> None:
        self._raise_for = raise_for
        self._ok = ok

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
        if corpus.corpus_id in self._raise_for:
            raise RuntimeError(f"backend down for {corpus.corpus_id}")
        return list(self._ok.get(corpus.corpus_id, []))


def _encoders() -> tuple[DenseEncoder, SparseEncoder]:
    from tests.fixtures import FakeDenseEncoder, FakeSparseEncoder

    return FakeDenseEncoder(), FakeSparseEncoder()


# -- merge_evidence --------------------------------------------------------------


def test_merge_dedup_by_evidence_id() -> None:
    a = _evidence("e1", "same text", corpus_id="product_docs", text_hash="H")
    b = _evidence("e1", "same text", corpus_id="engineering_wiki", text_hash="H")
    merged = merge_evidence({"product_docs": [a], "engineering_wiki": [b]})
    # Same id → one survivor; both corpora still recorded via contribution.
    assert len(merged) == 1
    assert merged[0].evidence_id == "e1"


def test_merge_folds_same_text_diff_version_not_collapsed() -> None:
    a = _evidence("e1", "identical", corpus_id="c1", document_version="v1", text_hash="H")
    b = _evidence("e2", "identical", corpus_id="c2", document_version="v2", text_hash="H")
    merged = merge_evidence({"c1": [a], "c2": [b]})
    # Different document_version is NOT folded.
    assert len(merged) == 2


def test_merge_folds_same_text_same_version_keeps_higher_authority() -> None:
    a = _evidence("e1", "identical", corpus_id="product_docs", text_hash="H", authority_level=80)
    b = _evidence("e2", "identical", corpus_id="tickets", text_hash="H", authority_level=40)
    merged = merge_evidence({"product_docs": [a], "tickets": [b]})
    # One primary survivor (higher authority = product_docs), both corpora recorded.
    assert len(merged) == 1
    assert merged[0].corpus_id == "product_docs"
    assert merged[0].authority_level == 80


def test_merge_deterministic_order() -> None:
    a = _evidence("e1", "t1", corpus_id="product_docs", text_hash="h1")
    b = _evidence("e2", "t2", corpus_id="tickets", text_hash="h2")
    merged1 = merge_evidence({"product_docs": [a], "tickets": [b]})
    merged2 = merge_evidence({"tickets": [b], "product_docs": [a]})
    assert [e.evidence_id for e in merged1] == [e.evidence_id for e in merged2]
    assert [e.evidence_id for e in merged1] == ["e1", "e2"]  # corpus_id asc order


# -- MultiCorpusRetrieval --------------------------------------------------------


def test_retrieve_calls_each_corpus_once() -> None:
    ev_a = _evidence("ea", "a", corpus_id="product_docs")
    ev_b = _evidence("eb", "b", corpus_id="engineering_wiki")
    retriever = _FakeRetriever({"product_docs": [ev_a], "engineering_wiki": [ev_b]})
    mc = MultiCorpusRetrieval(retriever)  # type: ignore[arg-type]
    de, se = _encoders()
    result = mc.retrieve(
        _ctx(),
        "q",
        [
            _corpus("product_docs", authority_level=80),
            _corpus("engineering_wiki", authority_level=70),
        ],
        dense_encoder=de,
        sparse_encoder=se,
    )
    assert set(result.corpora_used) == {"product_docs", "engineering_wiki"}
    assert len(result.evidence) == 2
    assert result.faults == ()
    assert result.insufficient_corpora == ()


def test_retrieve_single_corpus_only_one_call() -> None:
    ev = _evidence("ea", "a", corpus_id="product_docs")
    retriever = _FakeRetriever({"product_docs": [ev]})
    mc = MultiCorpusRetrieval(retriever)  # type: ignore[arg-type]
    de, se = _encoders()
    result = mc.retrieve(
        _ctx(), "q", [_corpus("product_docs")], dense_encoder=de, sparse_encoder=se
    )
    assert result.corpora_used == ("product_docs",)
    assert len(result.evidence) == 1


def test_retrieve_partial_fault_keeps_other_evidence() -> None:
    ev_ok = _evidence("eb", "b", corpus_id="engineering_wiki")
    retriever = _FaultyRetriever(raise_for={"product_docs"}, ok={"engineering_wiki": [ev_ok]})
    mc = MultiCorpusRetrieval(retriever)  # type: ignore[arg-type]
    de, se = _encoders()
    result = mc.retrieve(
        _ctx(),
        "q",
        [
            _corpus("product_docs", authority_level=80),
            _corpus("engineering_wiki", authority_level=70),
        ],
        dense_encoder=de,
        sparse_encoder=se,
    )
    # The healthy corpus still contributes; the faulted one is reported, not "no evidence".
    assert result.corpora_used == ("engineering_wiki",)
    assert len(result.evidence) == 1
    assert len(result.faults) == 1
    assert result.faults[0].corpus_id == "product_docs"
    assert result.faults[0].error_type == "RuntimeError"


def test_retrieve_total_fault_raises() -> None:
    retriever = _FaultyRetriever(raise_for={"product_docs", "tickets"}, ok={})
    mc = MultiCorpusRetrieval(retriever)  # type: ignore[arg-type]
    de, se = _encoders()
    try:
        mc.retrieve(
            _ctx(),
            "q",
            [_corpus("product_docs"), _corpus("tickets")],
            dense_encoder=de,
            sparse_encoder=se,
        )
        raise AssertionError("expected RuntimeError to propagate")
    except RuntimeError:
        pass


def test_retrieve_insufficient_corpus_recorded_not_used() -> None:
    retriever = _FakeRetriever({"product_docs": [], "tickets": []})
    mc = MultiCorpusRetrieval(retriever)  # type: ignore[arg-type]
    de, se = _encoders()
    result = mc.retrieve(
        _ctx(),
        "q",
        [_corpus("product_docs"), _corpus("tickets")],
        dense_encoder=de,
        sparse_encoder=se,
    )
    assert result.evidence == ()
    assert result.corpora_used == ()
    assert set(result.insufficient_corpora) == {"product_docs", "tickets"}
    assert result.faults == ()
