"""Unit tests for the idempotent IngestionJob / DocumentManager (build plan §10).

Hermetic: temp Metadata DB (sqlite), in-memory Qdrant, in-memory Parent Store,
deterministic fake encoders. No LLM / network / model download.
"""

import os
import tempfile

from qdrant_client import QdrantClient

from agentic_rag_enterprise.domain.ingestion import DocumentStatus, JobStatus
from agentic_rag_enterprise.ingestion.chunker import ParentChildChunker
from agentic_rag_enterprise.ingestion.job import (
    DocumentManager,
    IngestionJob,
    IngestionRequest,
    IngestionStatus,
)
from agentic_rag_enterprise.security.policy import ResourceAcl
from agentic_rag_enterprise.storage.metadata_store import (
    MetadataStore,
    VersionContentConflict,
)
from agentic_rag_enterprise.storage.parent_store import ParentStore
from agentic_rag_enterprise.storage.vector_store import VectorStore
from tests.fixtures import DENSE_DIM, FakeDenseEncoder, FakeSparseEncoder, acl_payload


def _tmp_db_path() -> str:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    return path


def _seed_corpus(store: MetadataStore, tenant_id: str = "t1", corpus_id: str = "eng") -> None:
    store._conn.execute(  # noqa: SLF001 - test helper reaches into raw conn
        """
        INSERT INTO corpus_registry (
            corpus_id, tenant_id, name, description, created_at, updated_at
        ) VALUES (?, ?, 'corpus', '', ?, ?)
        """,
        (corpus_id, tenant_id, "2024-01-01T00:00:00+00:00", "2024-01-01T00:00:00+00:00"),
    )


def _manager() -> tuple[DocumentManager, MetadataStore, VectorStore, ParentStore, str]:
    db_path = _tmp_db_path()
    store = MetadataStore(db_path)
    _seed_corpus(store)
    client = QdrantClient(location=":memory:")
    vector = VectorStore(client)
    vector.create_collection("eng", dense_size=DENSE_DIM)
    parents = ParentStore()
    manager = DocumentManager(
        metadata_store=store,
        vector_store=vector,
        parent_store=parents,
        chunker=ParentChildChunker(),
        dense_encoder=FakeDenseEncoder(),
        sparse_encoder=FakeSparseEncoder(),
    )
    return manager, store, vector, parents, db_path


def _request(*, job_id: str, version: str, content: str) -> IngestionRequest:
    return IngestionRequest(
        tenant_id="t1",
        corpus_id="eng",
        document_id="d1",
        document_version=version,
        content=content,
        acl=ResourceAcl(**acl_payload(tenant_id="t1", acl_scope="tenant", security_level="public")),
        job_id=job_id,
    )


def _count_qdrant_points(vector: VectorStore, corpus_id: str) -> int:
    return vector._client.count(corpus_id).count


def test_full_ingest_activates_version_and_writes_data_plane() -> None:
    manager, store, vector, parents, db_path = _manager()
    res = manager.ingest(_request(job_id="j1", version="v1", content="# T\n\nhello world"))
    assert res.status == IngestionStatus.INDEXED
    assert res.child_count > 0
    assert store.get_active_document("t1", "eng", "d1").version == "v1"
    assert store.get_job_status("j1") == JobStatus.SUCCEEDED
    # Data plane is populated and visible (active points present).
    assert _count_qdrant_points(vector, "eng") > 0
    assert len(parents._store) > 0
    store.close()
    os.unlink(db_path)


def test_idempotent_duplicate_job_returns_already_indexed() -> None:
    manager, store, vector, parents, db_path = _manager()
    req = _request(job_id="j1", version="v1", content="# T\n\nhello world")
    first = manager.ingest(req)
    assert first.status == IngestionStatus.INDEXED
    children_after_first = _count_qdrant_points(vector, "eng")

    # Same job_id re-delivered -> ALREADY_INDEXED, no new chunks/points.
    second = manager.ingest(req)
    assert second.status == IngestionStatus.ALREADY_INDEXED
    assert _count_qdrant_points(vector, "eng") == children_after_first
    assert store.get_job_status("j1") == JobStatus.SUCCEEDED
    store.close()
    os.unlink(db_path)


def test_new_version_switches_active_and_deprecates_old() -> None:
    manager, store, vector, parents, db_path = _manager()
    manager.ingest(_request(job_id="j1", version="v1", content="# T\n\nfirst version"))
    manager.ingest(_request(job_id="j2", version="v2", content="# T\n\nsecond version"))

    active = store.get_active_document("t1", "eng", "d1")
    assert active.version == "v2"
    old = store.get_document("t1", "eng", "d1", "v1")
    assert old.status == DocumentStatus.DEPRECATED
    assert old.deprecated is True
    # Active-version isolation: only one active row.
    assert store.get_current_revision("t1", "eng", "d1") == 2
    store.close()
    os.unlink(db_path)


def test_step_reentrancy_resumes_after_crash() -> None:
    manager, store, vector, parents, db_path = _manager()
    req = _request(job_id="j1", version="v1", content="# T\n\nhello world")
    # Crash after writing parents (before qdrant write + commit + publish).
    partial = IngestionJob(
        store=store,
        vector_store=vector,
        parent_store=parents,
        chunker=ParentChildChunker(),
        dense_encoder=FakeDenseEncoder(),
        sparse_encoder=FakeSparseEncoder(),
        request=req,
    ).run(max_step="write_parents")
    assert partial.status == IngestionStatus.IN_PROGRESS  # crashed before completion
    # New version not yet visible (no qdrant points / not committed).
    assert store.get_active_document("t1", "eng", "d1") is None

    # Resume: completes the remaining steps.
    final = manager.ingest(req)
    assert final.status == IngestionStatus.INDEXED
    assert store.get_active_document("t1", "eng", "d1").version == "v1"
    assert _count_qdrant_points(vector, "eng") > 0
    store.close()
    os.unlink(db_path)


def test_compensation_cleans_data_plane_before_commit() -> None:
    manager, store, vector, parents, db_path = _manager()
    client = vector._client

    class FailingVectorStore(VectorStore):
        def upsert(self, name: str, points):  # type: ignore[override]
            raise RuntimeError("simulated qdrant outage")

    failing = FailingVectorStore(client)
    req = _request(job_id="j1", version="v1", content="# T\n\nhello world")
    job = IngestionJob(
        store=store,
        vector_store=failing,
        parent_store=parents,
        chunker=ParentChildChunker(),
        dense_encoder=FakeDenseEncoder(),
        sparse_encoder=FakeSparseEncoder(),
        request=req,
    )
    res = job.run()
    # Pre-commit failure -> failed, compensation removed this version's artifacts.
    assert res.status == IngestionStatus.FAILED
    assert store.get_job_status("j1") == JobStatus.FAILED
    assert store.get_active_document("t1", "eng", "d1") is None
    assert len(parents._store) == 0  # parents written then compensated
    assert _count_qdrant_points(vector, "eng") == 0
    store.close()
    os.unlink(db_path)


def test_active_version_conflict_fails_closed() -> None:
    manager, store, vector, parents, db_path = _manager()
    req = _request(job_id="j1", version="v1", content="# T\n\nhello world")
    manager.ingest(req)
    # A competing commit with a stale expected_revision must not corrupt the
    # currently active version.
    import pytest

    with pytest.raises(Exception):
        store.commit_active_version(
            tenant_id="t1",
            corpus_id="eng",
            document_id="d1",
            new_version="v2",
            expected_revision=0,
        )
    assert store.get_active_document("t1", "eng", "d1").version == "v1"
    store.close()
    os.unlink(db_path)


def test_idempotency_is_keyed_on_document_version_not_job_id() -> None:
    # P1-2: same (document, version) + same content -> ALREADY_INDEXED even with
    # a DIFFERENT job_id, and must NOT flip the active row back to processing.
    manager, store, vector, parents, db_path = _manager()
    first = manager.ingest(_request(job_id="j1", version="v1", content="# T\n\nhello world"))
    assert first.status == IngestionStatus.INDEXED

    second = manager.ingest(_request(job_id="j2", version="v1", content="# T\n\nhello world"))
    assert second.status == IngestionStatus.ALREADY_INDEXED
    active = store.get_active_document("t1", "eng", "d1")
    assert active is not None
    assert active.status == DocumentStatus.ACTIVE
    assert active.version == "v1"
    store.close()
    os.unlink(db_path)


def test_same_version_different_content_is_rejected() -> None:
    # P1-2: re-ingesting an existing version with different content must not
    # overwrite the prior version.
    manager, store, vector, parents, db_path = _manager()
    manager.ingest(_request(job_id="j1", version="v1", content="# T\n\nfirst content"))
    import pytest

    with pytest.raises(VersionContentConflict):
        manager.ingest(_request(job_id="j2", version="v1", content="# T\n\nchanged content"))
    # Original version untouched and still active.
    assert store.get_active_document("t1", "eng", "d1").version == "v1"
    store.close()
    os.unlink(db_path)


def test_old_job_cannot_override_newer_committed_version() -> None:
    # P1-3: an older job (acquired at revision 1, then interrupted before
    # commit) must lose the race to a newer job that committed first.
    manager, store, vector, parents, db_path = _manager()
    manager.ingest(_request(job_id="j0", version="v1", content="# T\n\nv1 content"))
    # Job A (v2) acquires at current revision 1, then is interrupted before commit.
    IngestionJob(
        store=store,
        vector_store=vector,
        parent_store=parents,
        chunker=ParentChildChunker(),
        dense_encoder=FakeDenseEncoder(),
        sparse_encoder=FakeSparseEncoder(),
        request=_request(job_id="jA", version="v2", content="# T\n\nv2 content"),
    ).run(max_step="verify")
    # Job B (v3) acquires at revision 1 and commits first -> active v3, rev 2.
    manager.ingest(_request(job_id="jB", version="v3", content="# T\n\nv3 content"))
    assert store.get_active_document("t1", "eng", "d1").version == "v3"

    # Job A finally commits using its PERSISTED base_revision (1). Current is 2,
    # so CAS rejects it; A fails and compensates, leaving v3 active.
    res_a = IngestionJob(
        store=store,
        vector_store=vector,
        parent_store=parents,
        chunker=ParentChildChunker(),
        dense_encoder=FakeDenseEncoder(),
        sparse_encoder=FakeSparseEncoder(),
        request=_request(job_id="jA", version="v2", content="# T\n\nv2 content"),
    ).run()
    assert res_a.status == IngestionStatus.FAILED
    assert res_a.error_code == "active_version_conflict"
    assert store.get_active_document("t1", "eng", "d1").version == "v3"
    store.close()
    os.unlink(db_path)


def test_compensation_cleans_control_plane_when_verify_fails() -> None:
    # P1-5: a pre-commit failure after data-plane writes must also remove the
    # control-plane chunk records and mark the processing row failed (not
    # relying on in-memory state).
    manager, store, vector, parents, db_path = _manager()

    class VerifyFailingJob(IngestionJob):
        def _step_verify(self) -> None:
            raise RuntimeError("simulated verification failure")

    req = _request(job_id="j1", version="v1", content="# T\n\nhello world")
    failing = VerifyFailingJob(
        store=store,
        vector_store=vector,
        parent_store=parents,
        chunker=ParentChildChunker(),
        dense_encoder=FakeDenseEncoder(),
        sparse_encoder=FakeSparseEncoder(),
        request=req,
    )
    res = failing.run()
    assert res.status == IngestionStatus.FAILED
    # Control plane cleaned.
    assert store.list_chunk_records("t1", "eng", "d1", "v1") == []
    doc = store.get_document("t1", "eng", "d1", "v1")
    assert doc is not None
    assert doc.status == DocumentStatus.FAILED
    # Data plane cleaned too.
    assert len(parents._store) == 0
    assert _count_qdrant_points(vector, "eng") == 0
    store.close()
    os.unlink(db_path)


def test_already_indexed_resumes_after_commit_crash() -> None:
    # P1-1: a real crash between commit_active_version succeeding and the
    # "commit" step marker being written leaves the version ACTIVE but the job
    # still RUNNING. A re-delivery must RESUME and finish publish/finalize
    # (INDEXED), NOT short-circuit to ALREADY_INDEXED and silently skip the
    # data-plane promotion.
    manager, store, vector, parents, db_path = _manager()
    req = _request(job_id="j1", version="v1", content="# T\n\nhello world")
    first = manager.ingest(req)
    assert first.status == IngestionStatus.INDEXED

    # Simulate the crash: job back to RUNNING, commit step marker unwritten,
    # while the document is already active in the control plane.
    store.mark_job_terminal("j1", JobStatus.RUNNING)
    store.clear_steps("j1")
    store.mark_step("j1", "acquire", "done")  # acquire already happened
    assert store.get_active_document("t1", "eng", "d1").version == "v1"

    # Re-deliver: must resume and complete, not return ALREADY_INDEXED.
    second = manager.ingest(req)
    assert second.status == IngestionStatus.INDEXED
    assert store.get_job_status("j1") == JobStatus.SUCCEEDED
    assert _count_qdrant_points(vector, "eng") > 0
    store.close()
    os.unlink(db_path)


def test_verify_rejects_parent_identity_mismatch() -> None:
    # P1-5: _step_verify must detect a parent whose stored identity does not
    # match the request, not just column presence.
    import pytest

    manager, store, vector, parents, db_path = _manager()
    req = _request(job_id="j1", version="v1", content="# T\n\nhello world")
    job = IngestionJob(
        store=store,
        vector_store=vector,
        parent_store=parents,
        chunker=ParentChildChunker(),
        dense_encoder=FakeDenseEncoder(),
        sparse_encoder=FakeSparseEncoder(),
        request=req,
    )
    job.run(max_step="write_qdrant")

    # Tamper a stored parent's tenant_id -> identity mismatch.
    pid = next(iter(parents._store))
    chunk = parents.get(pid)
    parents.put(chunk.model_copy(update={"tenant_id": "other-tenant"}))

    with pytest.raises(RuntimeError):
        job._step_verify()
    store.close()
    os.unlink(db_path)
