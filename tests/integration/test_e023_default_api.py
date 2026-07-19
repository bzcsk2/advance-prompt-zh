"""E-023 P1-1 acceptance: the PUBLIC default API can create and resume a run.

Drives the REAL default ``POST /v1/chat`` (no dependency override) through
``TestClient(app)`` + ``get_default_container().service`` — exactly the path the
verdict required ("using get_default_container().service / TestClient(app), not a
hand-built service"). It proves:

* a ``run_id`` on the public endpoint produces a retrievable checkpoint on the
  shared Metadata DB (the endpoint formerly ignored ``run_id``);
* a later ``resume=true`` with the same ``run_id`` re-authorizes and recovers the
  run, returning an answer equal to the uninterrupted run (determinism).
"""

from __future__ import annotations

import os
import tempfile

from fastapi.testclient import TestClient
import pytest

from agentic_rag_enterprise.api.main import app
from agentic_rag_enterprise.security.policy import ResourceAcl
from agentic_rag_enterprise.services.container import (
    get_default_container,
    reset_default_container,
)


@pytest.fixture(autouse=True)
def _fresh_container() -> None:
    # E-023 P1-2 hermeticity fix: a per-test temp metadata DB so this suite does
    # not share the production ``metadata.db`` with other default-app tests.
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    reset_default_container(metadata_db_path=path)
    yield
    reset_default_container()
    try:
        os.unlink(path)
    except OSError:
        pass


_TENANT = "t1"
_CORPUS = "eng"
_DOC = "doc-planner"
_VERSION = "v1"
_CONTENT = """# System Overview

The planner selects corpora based on the question.

## Architecture

The runtime uses a planner and a sufficiency judge to decide when to abstain.
"""

# A stable session id is REQUIRED: the API derives ``session_id`` from the
# ``x-session-id`` header, and ``resume`` refuses a different session (principal
# mismatch). The same tenant / user / session / policy must be sent on both calls.
_SESSION = "sess-e023-p1-1"
_HEADERS = {
    "x-tenant-id": _TENANT,
    "x-user-id": "u1",
    "x-session-id": _SESSION,
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
        job_id="job-e023-p1-1",
    )
    assert result.status in ("indexed", "already_indexed"), result


def test_public_api_creates_resumable_checkpoint() -> None:
    _ingest()
    client = TestClient(app)
    resp = client.post(
        "/v1/chat",
        json={
            "query": "how does the planner work?",
            "corpus_id": _CORPUS,
            "run_id": "API-R1",
        },
        headers=_HEADERS,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["abstained"] is False

    # The PUBLIC run_id produced a checkpoint on the shared Metadata DB.
    row = (
        get_default_container()
        .metadata_store._conn.execute(
            "SELECT status FROM run_checkpoints WHERE run_id=?", ("API-R1",)
        )
        .fetchone()
    )
    assert row is not None
    assert row["status"] in ("running", "completed")


def test_public_api_resume_recovers_the_run() -> None:
    _ingest()
    client = TestClient(app)

    first = client.post(
        "/v1/chat",
        json={
            "query": "how does the planner work?",
            "corpus_id": _CORPUS,
            "run_id": "API-R2",
        },
        headers=_HEADERS,
    )
    assert first.status_code == 200
    first_data = first.json()

    # Resume the same run_id under the SAME principal/session/policy.
    resumed = client.post(
        "/v1/chat",
        json={
            "query": "how does the planner work?",
            "corpus_id": _CORPUS,
            "run_id": "API-R2",
            "resume": True,
        },
        headers=_HEADERS,
    )
    assert resumed.status_code == 200
    rd = resumed.json()

    # The resumed answer equals the uninterrupted run (determinism, invariant 4).
    assert rd["abstained"] == first_data["abstained"]
    assert rd["completeness"] == first_data["completeness"]
    assert {e["evidence_id"] for e in rd["evidence"]} == {
        e["evidence_id"] for e in first_data["evidence"]
    }
