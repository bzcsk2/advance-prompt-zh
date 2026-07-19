"""Unit tests for the E-014 ``POST /v1/chat`` FastAPI adapter.

Uses FastAPI's TestClient with the ``ChatService`` dependency overridden by a
fake, so no real Qdrant / model is touched. Asserts the adapter is
request-only (no business rules), never leaks ``denied_reasons``, always
returns the validated ``AnswerEnvelope`` (success or abstain), builds the
:class:`SecurityContext` from **trusted headers** (never the body), and returns
only **fixed generic** error messages (no internal identifiers leaked).
"""

from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from agentic_rag_enterprise.answer import Claim
from agentic_rag_enterprise.api.dependencies import get_chat_service
from agentic_rag_enterprise.api.main import app
from agentic_rag_enterprise.domain.corpus import CorpusConfig
from agentic_rag_enterprise.domain.evidence import Evidence as SnapshotEvidence
from agentic_rag_enterprise.domain.security import SecurityContext
from agentic_rag_enterprise.providers import FakeModel, ModelProfile
from agentic_rag_enterprise.services.chat_service import ChatService
from agentic_rag_enterprise.services.claims_schema import ClaimExtraction
from agentic_rag_enterprise.storage.vector_store import DenseEncoder, SparseEncoder
from tests.fixtures import FakeDenseEncoder, FakeSparseEncoder


@pytest.fixture(autouse=True)
def _clear_dependency_overrides() -> None:
    # Each test overrides get_chat_service; clear it afterwards so sibling test
    # modules (e.g. test_default_app, which exercises the REAL default service)
    # are not polluted by a leftover fake.
    yield
    app.dependency_overrides.clear()


# Trusted request headers the gateway injects (the API builds SecurityContext
# from these, never from the body).
_HEADERS = {
    "x-tenant-id": "t1",
    "x-user-id": "u1",
    "x-request-id": "r-test",
    "x-session-id": "s-test",
    "x-security-levels": "public,internal",
    "x-policy-version": "1.0",
}


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


class _FakeRetriever:
    def __init__(self, payload: object) -> None:
        self._payload = payload

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
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload  # type: ignore[return-value]


def _build_service(payload: object, extraction: ClaimExtraction) -> ChatService:
    model = FakeModel(
        profile=ModelProfile(provider="fake", model="fake-model", purpose="synthesis")
    )
    model.register_structured_factory(ClaimExtraction, lambda: extraction)
    return ChatService(
        retriever=_FakeRetriever(payload),
        dense_encoder=FakeDenseEncoder(),
        sparse_encoder=FakeSparseEncoder(),
        model=model,
        resolve_corpus=lambda cid: CorpusConfig(
            corpus_id=cid,
            tenant_id="t1",
            name="Eng",
            description="",
            domain="",
            owner="",
            source_type="wiki",
            capability_ids=[],
            security_policy_id="p",
            default_security_level="internal",
            created_at=datetime.now(),
            updated_at=datetime.now(),
        ),
    )


def _client(service: ChatService) -> TestClient:
    app.dependency_overrides[get_chat_service] = lambda: service
    return TestClient(app)


def _body(query: str = "q", **overrides: object) -> dict:
    # The body carries ONLY the question + corpus. No identity fields.
    body = {"query": query, "corpus_id": "eng"}
    body.update(overrides)
    return body


def test_v1_chat_sufficient_returns_envelope() -> None:
    extraction = ClaimExtraction(
        draft_answer="DRAFT PROSE",
        claims=[Claim(claim_id="c1", text="fact A", evidence_ids=("e1",))],
    )
    client = _client(_build_service([_evidence("e1")], extraction))
    resp = client.post("/v1/chat", json=_body(), headers=_HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert data["abstained"] is False
    assert data["iterations"] == 1
    assert data["tool_calls"] == 1
    assert data["corpora_used"] == ["eng"]
    assert data["answer_markdown"] == "fact A"  # from kept claims, not draft
    assert "DRAFT" not in data["answer_markdown"]
    # request_id comes from the trusted header, never the body.
    assert data["request_id"] == "r-test"
    assert "denied_reasons" not in data


def test_v1_chat_insufficient_returns_abstained() -> None:
    client = _client(_build_service([], ClaimExtraction(draft_answer="x", claims=[])))
    resp = client.post("/v1/chat", json=_body(), headers=_HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert data["abstained"] is True
    assert data["completeness"] == "insufficient"
    assert data["stop_reason"] == "no_evidence"
    assert "denied_reasons" not in data


def test_v1_chat_missing_required_query_returns_422() -> None:
    client = _client(
        _build_service([_evidence("e1")], ClaimExtraction(draft_answer="x", claims=[]))
    )
    body = _body()
    del body["query"]
    resp = client.post("/v1/chat", json=body, headers=_HEADERS)
    assert resp.status_code == 422


def test_v1_chat_body_identity_fields_are_ignored() -> None:
    # A client smuggles identity fields into the JSON body. They must be ignored
    # (the request model no longer carries them); the trusted header governs.
    client = _client(
        _build_service([_evidence("e1")], ClaimExtraction(draft_answer="x", claims=[]))
    )
    resp = client.post(
        "/v1/chat",
        json={
            "query": "q",
            "corpus_id": "eng",
            "tenant_id": "t9",
            "is_admin": True,
            "permissions": ["audit:everything"],
        },
        headers=_HEADERS,
    )
    assert resp.status_code == 200  # accepted; smuggled fields dropped


def test_v1_chat_backend_error_generic_503() -> None:
    client = _client(
        _build_service(
            RuntimeError("qdrant down boom"), ClaimExtraction(draft_answer="x", claims=[])
        )
    )
    resp = client.post("/v1/chat", json=_body(), headers=_HEADERS)
    assert resp.status_code == 503
    assert resp.json()["detail"] == "The retrieval backend is temporarily unavailable."
    # No internal detail leaks (no "qdrant", no exception text).
    assert "qdrant" not in resp.text.lower()


def test_v1_chat_model_error_generic_502() -> None:
    model = FakeModel(
        profile=ModelProfile(provider="fake", model="fake-model", purpose="synthesis")
    )
    model.register_structured_factory(ClaimExtraction, lambda: 1 / 0)  # raises on invoke
    service = ChatService(
        retriever=_FakeRetriever([_evidence("e1")]),
        dense_encoder=FakeDenseEncoder(),
        sparse_encoder=FakeSparseEncoder(),
        model=model,
        resolve_corpus=lambda cid: CorpusConfig(
            corpus_id=cid,
            tenant_id="t1",
            name="Eng",
            description="",
            domain="",
            owner="",
            source_type="wiki",
            capability_ids=[],
            security_policy_id="p",
            default_security_level="internal",
            created_at=datetime.now(),
            updated_at=datetime.now(),
        ),
    )
    client = _client(service)
    resp = client.post("/v1/chat", json=_body(), headers=_HEADERS)
    assert resp.status_code == 502
    assert resp.json()["detail"] == "The answer service is temporarily unavailable."


def test_v1_chat_error_response_does_not_leak_internals() -> None:
    # Cross-tenant evidence triggers a TenantBindingError deep in the builder.
    # The API must NOT leak the evidence id / tenant id; it returns a generic 500.
    client = _client(
        _build_service(
            [_evidence("secret-ev", tenant_id="t2")], ClaimExtraction(draft_answer="x", claims=[])
        )
    )
    resp = client.post("/v1/chat", json=_body(), headers=_HEADERS)
    assert resp.status_code == 500
    assert resp.json()["detail"] == "An internal error occurred."
    text = resp.text.lower()
    assert "secret-ev" not in text
    assert "t2" not in text


class _FakeMetaStore:
    """Minimal metadata store fake: no checkpoint ever exists (resume → not found)."""

    def load_run_checkpoint(self, run_id: str) -> object:
        return None

    def record_control_plane_finding(self, **_: object) -> None:
        return None


def _client_with_metadata(payload: object, extraction: ClaimExtraction) -> TestClient:
    service = _build_service(payload, extraction)
    service._metadata_store = _FakeMetaStore()  # type: ignore[assignment]
    return _client(service)


def test_v1_chat_resume_without_run_id_returns_400() -> None:
    # Per contract §E-023 (api/schemas.py): ``resume`` without ``run_id`` is a
    # client error; never synthesize from a non-existent checkpoint.
    client = _client_with_metadata([_evidence("e1")], ClaimExtraction(draft_answer="x", claims=[]))
    resp = client.post("/v1/chat", json=_body(resume=True), headers=_HEADERS)
    assert resp.status_code == 400
    assert resp.json()["detail"] == "Resume requested without a run_id."


def test_v1_chat_resume_unknown_run_id_returns_404_generic() -> None:
    # A resume for a missing / foreign checkpoint must NOT leak the reason; the
    # API maps ResumeAuthError to a fixed generic message at 404.
    client = _client_with_metadata([_evidence("e1")], ClaimExtraction(draft_answer="x", claims=[]))
    resp = client.post(
        "/v1/chat", json=_body(resume=True, run_id="does-not-exist"), headers=_HEADERS
    )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "An internal error occurred."
    assert "does-not-exist" not in resp.text.lower()
