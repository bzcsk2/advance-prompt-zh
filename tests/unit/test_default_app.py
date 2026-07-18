"""End-to-end test of the REAL default ``POST /v1/chat`` application.

Unlike ``test_chat_api.py`` (which overrides ``get_chat_service`` with a fake),
this test exercises the genuine default wiring: a shared, in-process
:class:`~agentic_rag_enterprise.services.container.DefaultServiceContainer`
backed by in-memory Qdrant, deterministic encoders, and a hermetic synthesis
model. A document is ingested through the *same* storage stack the chat service
uses, proving the run-chain (ingest -> retrieve -> answer/abstain) works with no
external dependency and that the default endpoint is actually runnable.
"""

from fastapi.testclient import TestClient
import pytest

from agentic_rag_enterprise.api.main import app
from agentic_rag_enterprise.security.policy import ResourceAcl
from agentic_rag_enterprise.services import container as _container_mod
from agentic_rag_enterprise.services.container import get_default_container


@pytest.fixture(autouse=True)
def _fresh_default_container():
    # The default container is a process-wide singleton shared by many tests.
    # Swap in a fresh, empty one for this test (and restore the prior view
    # afterwards) so the e2e run-chain is hermetic and not polluted by other
    # tests' ingested documents in the shared in-memory Qdrant / sqlite.
    saved = _container_mod._CONTAINER
    _container_mod.reset_default_container()
    yield
    _container_mod._CONTAINER = saved


_TENANT = "t1"
_CORPUS = "eng"
_DOC = "doc-1"
_VERSION = "v1"
_CONTENT = """# System Overview

The planner selects corpora based on the question.

## Architecture

The runtime uses a planner and a sufficiency judge to decide when to abstain.
"""

_SUFFICIENT_HEADERS = {
    "x-tenant-id": _TENANT,
    "x-user-id": "u1",
    "x-security-levels": "public,internal",
    "x-policy-version": "1.0",
}


def _ingest() -> None:
    acl = ResourceAcl(
        tenant_id=_TENANT,
        security_level="internal",
        acl_scope="tenant",
        allowed_user_ids=["u1"],
    )
    result = get_default_container().ingest(
        tenant_id=_TENANT,
        corpus_id=_CORPUS,
        document_id=_DOC,
        document_version=_VERSION,
        content=_CONTENT,
        acl=acl,
        job_id="job-default-e2e",
    )
    # Idempotent: the first ingest returns "indexed"; a later ingest of the same
    # artifact (same content hash) returns "already_indexed". Both leave the
    # document retrievable by the shared chat service.
    assert result.status in ("indexed", "already_indexed"), result


def test_default_service_is_runnable_without_override() -> None:
    # The default dependency must build a working service (no 500 on import /
    # first call) — this previously raised ChatServiceError for missing encoders.
    from agentic_rag_enterprise.api.dependencies import get_chat_service

    service = get_chat_service()
    assert service is not None


def test_default_app_ingest_then_chat_sufficient() -> None:
    _ingest()
    client = TestClient(app)  # no dependency_override: real default service
    resp = client.post(
        "/v1/chat",
        json={"query": "how does the planner work?", "corpus_id": _CORPUS},
        headers=_SUFFICIENT_HEADERS,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["abstained"] is False
    assert data["completeness"] == "complete"
    assert data["corpora_used"] == [_CORPUS]
    # Evidence + citations resolved from the ingested document.
    assert len(data["evidence"]) >= 1
    assert len(data["citations"]) >= 1
    # The retrieved Evidence is the ingested content (grounded, not a model draft).
    assert "planner" in data["evidence"][0]["text"].lower()
    assert data["answer_markdown"]


def test_default_app_body_identity_smuggling_is_ignored() -> None:
    # The client tries to assert a different tenant via the body. The header
    # (trusted gateway injection) governs; the corpus gate fails for tenant t2,
    # so the answer is an abstain — proving the body identity is not trusted.
    _ingest()
    client = TestClient(app)
    resp = client.post(
        "/v1/chat",
        json={
            "query": "how does the planner work?",
            "corpus_id": _CORPUS,
            "tenant_id": _TENANT,  # smuggled, ignored
            "is_admin": True,  # smuggled, ignored
        },
        headers={
            "x-tenant-id": "t2",  # trusted header wins
            "x-user-id": "u1",
            "x-security-levels": "public,internal",
            "x-policy-version": "1.0",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["abstained"] is True
    assert data["completeness"] == "insufficient"


def test_default_app_unknown_corpus_is_rejected() -> None:
    _ingest()
    client = TestClient(app)
    resp = client.post(
        "/v1/chat",
        json={"query": "anything", "corpus_id": "does-not-exist"},
        headers=_SUFFICIENT_HEADERS,
    )
    # An unknown corpus must not return a 200 success envelope (and must not
    # leak the corpus id in the body).
    assert resp.status_code in (400, 404, 500)
    assert "does-not-exist" not in resp.text.lower()
    assert "An internal error occurred." not in resp.text or resp.status_code == 500
