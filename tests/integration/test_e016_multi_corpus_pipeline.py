"""Integration tests: E-016 multi-corpus pipeline through ChatService.

Exercises the router → cross-corpus retrieval → merge/dedup → single-pass
synthesis path end-to-end with hermetic fakes, asserting:

* single-corpus request hits only that corpus;
* comparison request hits two authorized corpora and merges their evidence;
* identical text across two corpora is deduplicated to one primary while both
  corpora remain recorded in ``corpora_used``;
* a total retrieval fault surfaces as an error (never an abstain);
* ``AnswerEnvelope.corpora_used`` reflects only contributing corpora.
"""

from __future__ import annotations

from datetime import datetime

from agentic_rag_enterprise.corpus.registry import InMemoryCorpusRegistry
from agentic_rag_enterprise.domain.corpus import CorpusConfig
from agentic_rag_enterprise.domain.evidence import Evidence as SnapshotEvidence
from agentic_rag_enterprise.domain.security import SecurityContext
from agentic_rag_enterprise.retrieval.fast_path import FastPathBackendError
from agentic_rag_enterprise.services.chat_service import ChatService
from agentic_rag_enterprise.services.claims_schema import ClaimExtraction
from tests.fixtures import FakeDenseEncoder, FakeSparseEncoder


def _corpus(corpus_id: str, authority_level: int = 50) -> CorpusConfig:
    return CorpusConfig(
        corpus_id=corpus_id,
        tenant_id="local",
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
    corpus_id: str,
    *,
    authority_level: int = 50,
    text_hash: str | None = None,
) -> SnapshotEvidence:
    return SnapshotEvidence(
        evidence_id=evidence_id,
        tenant_id="local",
        corpus_id=corpus_id,
        document_id="d1",
        document_version="v1",
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


def _ctx(allowed_corpus_ids: list[str] | None = None) -> SecurityContext:
    return SecurityContext(
        request_id="r",
        session_id="s",
        tenant_id="local",
        user_id="u",
        policy_version="1.0",
        allowed_corpus_ids=allowed_corpus_ids,
    )


class _FakeRetriever:
    def __init__(self, per_corpus: dict[str, list[SnapshotEvidence]]) -> None:
        self._per_corpus = per_corpus
        self.calls: list[str] = []

    def retrieve_evidence(
        self,
        ctx: SecurityContext,
        query: str,
        corpus: CorpusConfig,
        top_k: object = None,
        *,
        dense_encoder: object,
        sparse_encoder: object,
        iteration: int = 0,
        plan_step_id: object = None,
    ) -> list[SnapshotEvidence]:
        self.calls.append(corpus.corpus_id)
        return list(self._per_corpus.get(corpus.corpus_id, []))


class _FaultyRetriever:
    def retrieve_evidence(
        self,
        ctx: SecurityContext,
        query: str,
        corpus: CorpusConfig,
        top_k: object = None,
        *,
        dense_encoder: object,
        sparse_encoder: object,
        iteration: int = 0,
        plan_step_id: object = None,
    ) -> list[SnapshotEvidence]:
        raise RuntimeError(f"backend down for {corpus.corpus_id}")


class _SynthesisModel:
    def with_structured_output(self, schema: object) -> "_SynthesisModel":
        return self

    def invoke(self, messages: object) -> object:
        return ClaimExtraction(draft_answer="merged answer", claims=[])


def _service(retriever: object, registry: InMemoryCorpusRegistry) -> ChatService:
    return ChatService(
        retriever=retriever,  # type: ignore[arg-type]
        dense_encoder=FakeDenseEncoder(),
        sparse_encoder=FakeSparseEncoder(),
        model=_SynthesisModel(),
        resolve_corpus=lambda cid: _corpus(cid),
        registry=registry,
    )


def test_single_corpus_request_calls_only_one() -> None:
    retriever = _FakeRetriever(
        {"product_docs": [_evidence("ep", "product evidence", "product_docs")]}
    )
    svc = _service(retriever, InMemoryCorpusRegistry())
    env = svc.answer_multi_corpus("q", _ctx(), corpus_ids=["product_docs"])
    assert retriever.calls == ["product_docs"]
    assert env.corpora_used == ("product_docs",)
    assert not env.abstained


def test_comparison_request_merges_two_corpora() -> None:
    retriever = _FakeRetriever(
        {
            "product_docs": [
                _evidence("ep", "product evidence", "product_docs", authority_level=80)
            ],
            "engineering_wiki": [
                _evidence("ee", "eng evidence", "engineering_wiki", authority_level=70)
            ],
        }
    )
    svc = _service(retriever, InMemoryCorpusRegistry())
    env = svc.answer_multi_corpus(
        "compare", _ctx(), corpus_ids=["product_docs", "engineering_wiki"]
    )
    assert set(retriever.calls) == {"product_docs", "engineering_wiki"}
    assert set(env.corpora_used) == {"product_docs", "engineering_wiki"}
    assert len(env.evidence) == 2
    assert not env.abstained


def test_identical_text_deduped_but_both_corpora_recorded() -> None:
    retriever = _FakeRetriever(
        {
            "product_docs": [
                _evidence(
                    "ep",
                    "identical text",
                    "product_docs",
                    authority_level=80,
                    text_hash="same",
                )
            ],
            "tickets": [
                _evidence("et", "identical text", "tickets", authority_level=40, text_hash="same")
            ],
        }
    )
    svc = _service(retriever, InMemoryCorpusRegistry())
    env = svc.answer_multi_corpus("compare", _ctx(), corpus_ids=["product_docs", "tickets"])
    # One primary Evidence (higher authority = product_docs), but both corpora
    # recorded in corpora_used (source attribution preserved).
    assert len(env.evidence) == 1
    assert env.evidence[0].corpus_id == "product_docs"
    assert set(env.corpora_used) == {"product_docs", "tickets"}


def test_total_fault_raises_not_abstains() -> None:
    svc = _service(_FaultyRetriever(), InMemoryCorpusRegistry())
    try:
        svc.answer_multi_corpus("q", _ctx(), corpus_ids=["product_docs", "engineering_wiki"])
        raise AssertionError("expected FastPathBackendError")
    except FastPathBackendError:
        pass


def test_empty_evidence_abstains() -> None:
    retriever = _FakeRetriever({})  # no corpus returns evidence
    svc = _service(retriever, InMemoryCorpusRegistry())
    env = svc.answer_multi_corpus("q", _ctx(), corpus_ids=["product_docs", "engineering_wiki"])
    assert env.abstained
    assert env.completeness == "insufficient"
    assert env.stop_reason == "no_evidence"
