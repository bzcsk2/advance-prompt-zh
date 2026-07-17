"""Unit tests for the E-014 chat application service.

Hermetic: a fake ``SecureRetriever`` and ``FakeModel`` stand in for Qdrant and the
LLM, so no real storage or model is touched. The tests assert the E-014 contract:

* sufficient  → build_answer_envelope from verified claims (draft prose discarded);
* insufficient → conservative_refusal (abstained);
* unsupported / empty claims → fail closed, draft never returned;
* tenant/evidence mismatch → typed error propagates, never a fabricated answer;
* backend / model fault → typed error propagates, never a refusal or answer;
* the model prompt carries only query + evidence, never security context fields.
"""

from datetime import datetime

import pytest

from agentic_rag_enterprise.answer import Claim
from agentic_rag_enterprise.domain.corpus import CorpusConfig
from agentic_rag_enterprise.domain.evidence import Evidence as SnapshotEvidence
from agentic_rag_enterprise.domain.security import SecurityContext
from agentic_rag_enterprise.providers import FakeModel, ModelProfile, ModelProvider
from agentic_rag_enterprise.retrieval.fast_path import FastPathBackendError
from agentic_rag_enterprise.retrieval.models import CorpusNotDiscoverableError
from agentic_rag_enterprise.answer.envelope import TenantBindingError
from agentic_rag_enterprise.services.chat_service import (
    ChatService,
    ModelInvocationError,
)
from agentic_rag_enterprise.services.claims_schema import ClaimExtraction
from agentic_rag_enterprise.storage.vector_store import DenseEncoder, SparseEncoder
from tests.fixtures import FakeDenseEncoder, FakeSparseEncoder


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


def _evidence(evidence_id: str = "e1", tenant_id: str = "t1") -> SnapshotEvidence:
    return SnapshotEvidence(
        evidence_id=evidence_id,
        tenant_id=tenant_id,
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


def _ctx(tenant_id: str = "t1", user_id: str = "u1") -> SecurityContext:
    return SecurityContext(
        request_id="r1",
        session_id="s1",
        tenant_id=tenant_id,
        user_id=user_id,
        policy_version="1.0",
    )


class _FakeRetriever:
    """Records the single retrieve_evidence call; optionally enforces the
    corpus-discoverability gate like the real SecureRetriever."""

    def __init__(self, payload: object, *, enforce_corpus_tenant: bool = True) -> None:
        self._payload = payload
        self.enforce_corpus_tenant = enforce_corpus_tenant
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
        if self.enforce_corpus_tenant and corpus.tenant_id != ctx.tenant_id:
            raise CorpusNotDiscoverableError(
                f"corpus {corpus.corpus_id} not discoverable for {ctx.tenant_id}"
            )
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload  # type: ignore[return-value]


class _RecordingModel:
    """ModelProvider that records the prompt and returns a fixed extraction."""

    def __init__(self, extraction: ClaimExtraction) -> None:
        self._extraction = extraction
        self.last_messages: list[dict[str, str]] | None = None

    def invoke(self, messages: list[dict[str, str]], **kwargs: object) -> str:
        return ""

    def with_structured_output(self, schema: type, **kwargs: object) -> object:
        return self._Wrapper(self, schema)

    class _Wrapper:
        def __init__(self, model: "_RecordingModel", schema: type) -> None:
            self._model = model
            self._schema = schema

        def invoke(self, messages: list[dict[str, str]], **kwargs: object):
            self._model.last_messages = messages
            return self._model._extraction


def _fake_model(extraction: ClaimExtraction) -> FakeModel:
    model = FakeModel(
        profile=ModelProfile(provider="fake", model="fake-model", purpose="synthesis")
    )
    model.register_structured_factory(ClaimExtraction, lambda: extraction)
    return model


def _service(
    retriever: _FakeRetriever,
    model: ModelProvider,
    resolve_corpus=None,
) -> ChatService:
    return ChatService(
        retriever=retriever,
        dense_encoder=FakeDenseEncoder(),
        sparse_encoder=FakeSparseEncoder(),
        model=model,
        resolve_corpus=resolve_corpus or (lambda cid: _corpus(corpus_id=cid, tenant_id="t1")),
    )


def test_sufficient_path_returns_envelope_from_verified_claims() -> None:
    retriever = _FakeRetriever([_evidence("e1"), _evidence("e2")])
    extraction = ClaimExtraction(
        draft_answer="DRAFT PROSE that must not appear",
        claims=[
            Claim(claim_id="c1", text="fact A", evidence_ids=("e1",)),
            Claim(claim_id="c2", text="fact B", evidence_ids=("e2",)),
        ],
    )
    env = _service(retriever, _fake_model(extraction)).answer("q", _ctx(), "eng")

    assert env.abstained is False
    assert env.completeness == "complete"
    assert env.iterations == 1
    assert env.tool_calls == 1
    assert env.corpora_used == ("eng",)
    # Answer rendered from kept claims, NOT the LLM draft prose.
    assert env.answer_markdown == "fact A\nfact B"
    assert "DRAFT" not in env.answer_markdown
    assert retriever.calls  # exactly one retrieve_evidence


def test_insufficient_path_returns_abstained_refusal() -> None:
    retriever = _FakeRetriever([])
    env = _service(retriever, _fake_model(ClaimExtraction(draft_answer="x", claims=[]))).answer(
        "q", _ctx(), "eng"
    )

    assert env.abstained is True
    assert env.claims == ()
    assert env.evidence == ()
    assert env.completeness == "insufficient"
    assert env.confidence == "low"
    assert env.stop_reason == "no_evidence"
    assert "DRAFT" not in env.answer_markdown


def test_unsupported_claims_removed_and_draft_not_returned() -> None:
    retriever = _FakeRetriever([_evidence("e1")])
    extraction = ClaimExtraction(
        draft_answer="DRAFT",
        claims=[
            Claim(
                claim_id="c1",
                text="unsupported secret fact",
                support_status="unsupported",
                evidence_ids=("e1",),
            )
        ],
    )
    env = _service(retriever, _fake_model(extraction)).answer("q", _ctx(), "eng")

    assert "unsupported secret fact" not in env.answer_markdown
    assert "DRAFT" not in env.answer_markdown
    # No kept claim survives → generic partial answer.
    assert env.completeness == "partial"
    assert env.answer_markdown == (
        "No supported claim could be established from the available evidence."
    )


def test_empty_claims_fail_closed() -> None:
    retriever = _FakeRetriever([_evidence("e1")])
    extraction = ClaimExtraction(draft_answer="DRAFT PROSE", claims=[])
    env = _service(retriever, _fake_model(extraction)).answer("q", _ctx(), "eng")

    assert "DRAFT" not in env.answer_markdown
    assert env.completeness == "partial"
    assert env.answer_markdown == (
        "No supported claim could be established from the available evidence."
    )


def test_corpus_tenant_mismatch_propagates_as_backend_error() -> None:
    # ctx tenant t2 vs corpus tenant t1 → SecureRetriever raises
    # CorpusNotDiscoverableError, which run_fast_path wraps as FastPathBackendError.
    retriever = _FakeRetriever([_evidence("e1")], enforce_corpus_tenant=True)
    with pytest.raises(FastPathBackendError):
        _service(retriever, _fake_model(ClaimExtraction(draft_answer="x", claims=[]))).answer(
            "q", _ctx(tenant_id="t2"), "eng"
        )


def test_evidence_tenant_mismatch_propagates_as_tenant_binding_error() -> None:
    # corpus/ctx match, but evidence belongs to a different tenant → E-013
    # TenantBindingError (fail-closed, no fabricated answer).
    retriever = _FakeRetriever([_evidence("e1", tenant_id="t2")], enforce_corpus_tenant=False)
    with pytest.raises(TenantBindingError):
        _service(retriever, _fake_model(ClaimExtraction(draft_answer="x", claims=[]))).answer(
            "q", _ctx(tenant_id="t1"), "eng"
        )


def test_backend_error_propagates_not_refusal() -> None:
    retriever = _FakeRetriever(RuntimeError("qdrant down"))
    with pytest.raises(FastPathBackendError):
        _service(retriever, _fake_model(ClaimExtraction(draft_answer="x", claims=[]))).answer(
            "q", _ctx(), "eng"
        )


def test_model_error_propagates() -> None:
    retriever = _FakeRetriever([_evidence("e1")])
    model = FakeModel(
        profile=ModelProfile(provider="fake", model="fake-model", purpose="synthesis")
    )
    model.register_structured_factory(ClaimExtraction, lambda: 1 / 0)  # raises on invoke
    with pytest.raises(ModelInvocationError):
        _service(retriever, model).answer("q", _ctx(), "eng")


def test_security_context_never_from_model() -> None:
    retriever = _FakeRetriever([_evidence("e1")])
    secret_ctx = SecurityContext(
        request_id="req-SECRET",
        session_id="ses-SECRET",
        tenant_id="t1",
        user_id="u-SECRET",
        policy_version="1.0",
    )
    extraction = ClaimExtraction(
        draft_answer="DRAFT",
        claims=[Claim(claim_id="c1", text="fact A", evidence_ids=("e1",))],
    )
    model = _RecordingModel(extraction)
    env = _service(retriever, model).answer("what is the policy?", secret_ctx, "eng")

    # The prompt carries the query + evidence, but never security fields.
    assert model.last_messages is not None
    blob = "\n".join(m["content"] for m in model.last_messages)
    assert "what is the policy?" in blob
    assert "some grounded body" in blob
    assert "u-SECRET" not in blob
    assert "req-SECRET" not in blob
    assert "policy_version" not in blob.lower()
    # The injected SecurityContext (not model output) drives the envelope.
    assert env.request_id == "req-SECRET"
