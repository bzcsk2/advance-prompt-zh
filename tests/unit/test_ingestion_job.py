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

    # Resume: completes the remaining steps (explicit recovery of a crashed
    # attempt on the still-RUNNING lease).
    final = manager.ingest(req, recover=True)
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
    ).run(recover=True)
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


def test_precise_commit_crash_resumes_publish_and_finalize() -> None:
    # P2 (precise crash hook): a real crash AFTER commit_active_version
    # succeeds but BEFORE the outer "commit" step marker is written must leave
    # the version ACTIVE in the control plane while the data plane is still
    # 'processing'. A re-delivery must RESUME and finish publish/finalize
    # (INDEXED) and clean the previously-replaced version's data plane, NOT
    # short-circuit to ALREADY_INDEXED and NOT compensate the committed version.
    manager, store, vector, parents, db_path = _manager()
    manager.ingest(_request(job_id="j0", version="v1", content="# T\n\nfirst version"))

    class CommitCrashJob(IngestionJob):
        def _step_commit(self) -> None:
            # Run the real commit (switches the active version), then crash
            # before the outer mark_step("commit") is written.
            super()._step_commit()
            raise RuntimeError("simulated crash after commit")

    req2 = _request(job_id="j2", version="v2", content="# T\n\nsecond version")
    crashed = CommitCrashJob(
        store=store,
        vector_store=vector,
        parent_store=parents,
        chunker=ParentChildChunker(),
        dense_encoder=FakeDenseEncoder(),
        sparse_encoder=FakeSparseEncoder(),
        request=req2,
    ).run()

    # Crashed before the commit marker: version committed (active) but the job
    # is FAILED and the data plane is still 'processing' (unpublished).
    assert crashed.status == IngestionStatus.FAILED
    assert store.get_job_status("j2") == JobStatus.FAILED
    assert store.get_active_document("t1", "eng", "d1").version == "v2"
    # The replaced v1 is deprecated in the control plane but its data plane is
    # not yet cleaned (publish never ran).
    v1_parents_before = [c for c in parents._store.values() if c.document_version == "v1"]
    assert v1_parents_before

    # Resume: must finish publish + finalize and clean the old data plane.
    resumed = manager.ingest(req2)
    assert resumed.status == IngestionStatus.INDEXED
    assert store.get_job_status("j2") == JobStatus.SUCCEEDED
    assert _count_qdrant_points(vector, "eng") > 0
    v1_parents_after = [c for c in parents._store.values() if c.document_version == "v1"]
    assert v1_parents_after
    assert all(c.metadata.get("deprecated") for c in v1_parents_after)
    store.close()
    os.unlink(db_path)


def test_build_conflict_loser_never_compensates() -> None:
    # P1-1: a BuildConflict loser must NOT mutate the shared data plane and must
    # NOT compensate (delete) the winning build's deterministic-ID artifacts.
    manager, store, vector, parents, db_path = _manager()
    req = _request(job_id="j1", version="v1", content="# T\n\nhello world")

    # Winner claims the lease and writes its data plane (not yet committed).
    winner = IngestionJob(
        store=store,
        vector_store=vector,
        parent_store=parents,
        chunker=ParentChildChunker(),
        dense_encoder=FakeDenseEncoder(),
        sparse_encoder=FakeSparseEncoder(),
        request=req,
    )
    winner.run(max_step="write_qdrant")
    points_before = _count_qdrant_points(vector, "eng")
    assert points_before > 0
    assert store.get_job_status("j1") == JobStatus.RUNNING

    # Concurrent loser (same artifact, different job_id) is rejected with
    # BuildConflict and must not delete the winner's data.
    loser = IngestionJob(
        store=store,
        vector_store=vector,
        parent_store=parents,
        chunker=ParentChildChunker(),
        dense_encoder=FakeDenseEncoder(),
        sparse_encoder=FakeSparseEncoder(),
        request=_request(job_id="j2", version="v1", content="# T\n\nhello world"),
    )
    res = loser.run()
    assert res.status == IngestionStatus.BUILD_CONFLICT
    assert res.error_code == "build_conflict"
    # Winner's data plane is intact; winner still owns the lease.
    assert _count_qdrant_points(vector, "eng") == points_before
    assert store.get_build_owner("t1", "eng", "d1", "v1") == "j1"
    assert store.get_job_status("j1") == JobStatus.RUNNING
    store.close()
    os.unlink(db_path)


def test_build_lease_fencing_blocks_taken_over_owner() -> None:
    # P1-2: once a failed build's lease is taken over by a concurrent delivery,
    # the original (stale) owner is fenced out — re-running it raises
    # BuildConflict and never compensates the new owner's data plane.
    manager, store, vector, parents, db_path = _manager()
    content = "# T\n\nhello world"
    req1 = _request(job_id="j1", version="v1", content=content)

    # j1 claims the lease and writes its data plane.
    IngestionJob(
        store=store,
        vector_store=vector,
        parent_store=parents,
        chunker=ParentChildChunker(),
        dense_encoder=FakeDenseEncoder(),
        sparse_encoder=FakeSparseEncoder(),
        request=req1,
    ).run(max_step="write_qdrant")
    points_after_j1 = _count_qdrant_points(vector, "eng")
    assert points_after_j1 > 0

    # j1's build fails (crash) -> terminal; lease still references j1.
    store.mark_job_terminal("j1", JobStatus.FAILED)

    # A concurrent delivery j2 takes over the lease and is IN-FLIGHT (running).
    IngestionJob(
        store=store,
        vector_store=vector,
        parent_store=parents,
        chunker=ParentChildChunker(),
        dense_encoder=FakeDenseEncoder(),
        sparse_encoder=FakeSparseEncoder(),
        request=_request(job_id="j2", version="v1", content=content),
    ).run(max_step="write_qdrant")
    assert store.get_build_owner("t1", "eng", "d1", "v1") == "j2"
    assert store.get_job_status("j2") == JobStatus.RUNNING

    # The original owner j1, now taken over by an in-flight j2, must be fenced
    # out (BuildConflict), never compensated, and never corrupt j2's data.
    res1b = IngestionJob(
        store=store,
        vector_store=vector,
        parent_store=parents,
        chunker=ParentChildChunker(),
        dense_encoder=FakeDenseEncoder(),
        sparse_encoder=FakeSparseEncoder(),
        request=req1,
    ).run()
    assert res1b.status == IngestionStatus.BUILD_CONFLICT
    assert res1b.error_code == "build_conflict"
    # j2's data plane is intact (not deleted by j1's compensation).
    assert _count_qdrant_points(vector, "eng") == points_after_j1
    store.close()
    os.unlink(db_path)


def test_verify_rejects_qdrant_payload_mismatch() -> None:
    # P1-3: _step_verify must detect an EXACT Qdrant payload mismatch
    # (e.g. a tampered parent_id), not merely a non-empty field.
    import pytest

    from agentic_rag_enterprise.storage.vector_store import child_chunk_to_point

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

    # Tamper a Qdrant point's parent_id in the data plane.
    child = job._children_list[0]
    tampered_point = child_chunk_to_point(
        child,
        req.acl,
        status="processing",
        deprecated=False,
        dense_encoder=FakeDenseEncoder(),
        sparse_encoder=FakeSparseEncoder(),
    )
    tampered_point.payload["parent_id"] = "wrong-parent"
    vector.upsert("eng", [tampered_point])

    with pytest.raises(RuntimeError):
        job._step_verify()
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
