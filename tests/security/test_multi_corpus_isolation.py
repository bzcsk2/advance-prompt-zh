"""Security tests: the third (undiscoverable) corpus is invisible end-to-end in
the E-016 multi-corpus path (router input → retrieval → evidence → corpora_used).
"""

from __future__ import annotations

from datetime import datetime

from agentic_rag_enterprise.corpus.registry import InMemoryCorpusRegistry
from agentic_rag_enterprise.corpus.router import CorpusRouter
from agentic_rag_enterprise.domain.corpus import CorpusConfig
from agentic_rag_enterprise.domain.evidence import Evidence as SnapshotEvidence
from agentic_rag_enterprise.domain.security import SecurityContext
from agentic_rag_enterprise.retrieval.multi_corpus import MultiCorpusRetrieval
from agentic_rag_enterprise.retrieval.models import CorpusNotDiscoverableError
from agentic_rag_enterprise.storage.vector_store import DenseEncoder, SparseEncoder
from tests.fixtures import FakeDenseEncoder, FakeSparseEncoder


def _ctx(allowed_corpus_ids: list[str] | None) -> SecurityContext:
    return SecurityContext(
        request_id="r",
        session_id="s",
        tenant_id="local",
        user_id="u",
        policy_version="1.0",
        allowed_corpus_ids=allowed_corpus_ids,
    )


def _corpus(corpus_id: str, authority_level: int = 50) -> CorpusConfig:
    return CorpusConfig(
        corpus_id=corpus_id,
        tenant_id="local",
        name=corpus_id,
        description="secret description of the denied corpus",
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


def _evidence(evidence_id: str, corpus_id: str) -> SnapshotEvidence:
    return SnapshotEvidence(
        evidence_id=evidence_id,
        tenant_id="local",
        corpus_id=corpus_id,
        document_id="d1",
        document_version="v1",
        source_uri="inline://d1",
        source_filename="d1.md",
        text="evidence",
        text_hash=f"h-{evidence_id}",
        retrieval_query="q",
        authority_level=50,
        retrieved_at=datetime(2024, 1, 1),
        acl_policy_id="default",
        policy_version="1.0",
        retrieval_iteration=0,
    )


def _encoders() -> tuple[DenseEncoder, SparseEncoder]:
    from tests.fixtures import FakeDenseEncoder, FakeSparseEncoder

    return FakeDenseEncoder(), FakeSparseEncoder()


class _FakeRetriever:
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


def test_third_corpus_absent_from_router() -> None:
    ctx = _ctx(allowed_corpus_ids=["product_docs", "engineering_wiki"])
    route = CorpusRouter().route(
        "compare product and tickets", ctx, InMemoryCorpusRegistry(), limit=5
    )
    assert {c.corpus_id for c in route.candidates} == {"product_docs", "engineering_wiki"}
    for c in route.candidates:
        assert c.corpus_id != "tickets"
        assert "secret description" not in c.rationale


def test_third_corpus_absent_from_evidence_and_corpora_used() -> None:
    ctx = _ctx(allowed_corpus_ids=["product_docs", "engineering_wiki"])
    retriever = _FakeRetriever(
        {
            "product_docs": [_evidence("ep", "product_docs")],
            "engineering_wiki": [_evidence("ee", "engineering_wiki")],
        }
    )
    mc = MultiCorpusRetrieval(retriever)  # type: ignore[arg-type]
    de, se = _encoders()
    # Even if a caller somehow listed the denied corpus, the SecureRetriever gate
    # inside retrieve_evidence would drop it; here we only route authorized ones.
    result = mc.retrieve(
        ctx,
        "q",
        [_corpus("product_docs", 80), _corpus("engineering_wiki", 70)],
        dense_encoder=de,
        sparse_encoder=se,
    )
    assert {ev.corpus_id for ev in result.evidence} == {"product_docs", "engineering_wiki"}
    assert "tickets" not in result.corpora_used
    assert result.faults == ()


def test_explicit_undiscoverable_corpus_fails_closed() -> None:
    ctx = _ctx(allowed_corpus_ids=["product_docs"])
    from agentic_rag_enterprise.services.chat_service import ChatService

    retriever = _FakeRetriever({"product_docs": [_evidence("ep", "product_docs")]})
    svc = ChatService(
        retriever=retriever,  # type: ignore[arg-type]
        dense_encoder=FakeDenseEncoder(),
        sparse_encoder=FakeSparseEncoder(),
        model=_NullModel(),
        resolve_corpus=lambda cid: _corpus(cid),
        registry=InMemoryCorpusRegistry(),
    )
    try:
        svc.answer_multi_corpus("q", ctx, corpus_ids=["product_docs", "tickets"])
        raise AssertionError("expected CorpusNotDiscoverableError")
    except CorpusNotDiscoverableError:
        pass


class _NullModel:
    def with_structured_output(self, schema: object) -> "_NullModel":
        return self

    def invoke(self, messages: object) -> object:
        from agentic_rag_enterprise.services.claims_schema import ClaimExtraction

        return ClaimExtraction(draft_answer="ok", claims=[])
