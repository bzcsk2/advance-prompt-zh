"""Crash-point tests for the E-008 active-version protocol (build plan §10.10).

Each test stops the job at a defined step (``max_step``) to simulate a crash,
asserts the control plane / data plane invariant that holds mid-flight, then
resumes and asserts the finished state is consistent. Hermetic: temp metadata
DB, in-memory Qdrant, fake encoders.
"""

import os
import tempfile

from qdrant_client import QdrantClient

from agentic_rag_enterprise.domain.corpus import CorpusConfig
from agentic_rag_enterprise.domain.ingestion import JobStatus
from agentic_rag_enterprise.ingestion.chunker import ParentChildChunker
from agentic_rag_enterprise.ingestion.job import (
    DocumentManager,
    IngestionJob,
    IngestionRequest,
    IngestionStatus,
)
from agentic_rag_enterprise.retrieval.hybrid import _HybridSearchAdapter
from agentic_rag_enterprise.retrieval.parent_reader import ParentReader
from agentic_rag_enterprise.retrieval.retriever import SecureRetriever
from agentic_rag_enterprise.security.policy import ResourceAcl
from agentic_rag_enterprise.storage.metadata_store import (
    JobIdentityConflict,
    MetadataStore,
)
from agentic_rag_enterprise.storage.parent_store import ParentStore
from agentic_rag_enterprise.storage.vector_store import VectorStore
from tests.fixtures import (
    DENSE_DIM,
    FakeDenseEncoder,
    FakeSparseEncoder,
    SAMPLE_MARKDOWN,
    acl_payload,
    make_security_context,
)


def _tmp_db_path() -> str:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    return path


def _seed_corpus(store: MetadataStore, tenant_id: str = "t1", corpus_id: str = "eng") -> None:
    store._conn.execute(  # noqa: SLF001
        """
        INSERT INTO corpus_registry (
            corpus_id, tenant_id, name, description, created_at, updated_at
        ) VALUES (?, ?, 'corpus', '', ?, ?)
        """,
        (corpus_id, tenant_id, "2024-01-01T00:00:00+00:00", "2024-01-01T00:00:00+00:00"),
    )


def _corpus() -> CorpusConfig:
    from datetime import datetime

    return CorpusConfig(
        corpus_id="eng",
        tenant_id="t1",
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


def _build():
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
    retriever = SecureRetriever(
        _HybridSearchAdapter(vector), ParentReader(parents), metadata_store=store
    )
    return manager, store, vector, parents, retriever, db_path


def _request(*, job_id: str, version: str, content: str) -> IngestionRequest:
    return IngestionRequest(
        tenant_id="t1",
        corpus_id="eng",
        document_id="doc1",
        document_version=version,
        content=content,
        acl=ResourceAcl(**acl_payload(tenant_id="t1", acl_scope="tenant", security_level="public")),
        job_id=job_id,
    )


def _retrieve_versions(retriever, query: str = "architecture planner security") -> list[str]:
    result = retriever.retrieve(
        make_security_context(),
        query,
        _corpus(),
        dense_encoder=FakeDenseEncoder(),
        sparse_encoder=FakeSparseEncoder(),
    )
    return [p.document_version for _, p in result.hits]


def test_crash_after_parent_write_recovers() -> None:
    manager, store, vector, parents, retriever, db_path = _build()
    req = _request(job_id="j1", version="v1", content=SAMPLE_MARKDOWN)
    # Crash after parents written, before qdrant write / commit / publish.
    IngestionJob(
        store=store,
        vector_store=vector,
        parent_store=parents,
        chunker=ParentChildChunker(),
        dense_encoder=FakeDenseEncoder(),
        sparse_encoder=FakeSparseEncoder(),
        request=req,
    ).run(max_step="write_parents")

    # Mid-flight invariant: not committed -> not visible to retrieval.
    assert store.get_active_document("t1", "eng", "doc1") is None
    assert _retrieve_versions(retriever) == []

    # Resume to completion.
    manager.ingest(req)
    assert store.get_active_document("t1", "eng", "doc1").version == "v1"
    assert "v1" in _retrieve_versions(retriever)
    store.close()
    os.unlink(db_path)


def test_crash_after_qdrant_write_keeps_old_active() -> None:
    manager, store, vector, parents, retriever, db_path = _build()
    manager.ingest(_request(job_id="j1", version="v1", content=SAMPLE_MARKDOWN))
    assert _retrieve_versions(retriever) == ["v1"]

    # Crash v2 after qdrant write (points are 'processing', not yet published).
    req2 = _request(job_id="j2", version="v2", content=SAMPLE_MARKDOWN + "\n\n## Extra\n")
    IngestionJob(
        store=store,
        vector_store=vector,
        parent_store=parents,
        chunker=ParentChildChunker(),
        dense_encoder=FakeDenseEncoder(),
        sparse_encoder=FakeSparseEncoder(),
        request=req2,
    ).run(max_step="write_qdrant")

    # Invariant: new version (processing) invisible; only old active returned.
    assert store.get_active_document("t1", "eng", "doc1").version == "v1"
    assert _retrieve_versions(retriever) == ["v1"]

    # Resume.
    manager.ingest(req2)
    assert store.get_active_document("t1", "eng", "doc1").version == "v2"
    assert _retrieve_versions(retriever) == ["v2"]
    store.close()
    os.unlink(db_path)


def test_crash_after_commit_publish_recovers_without_rollback() -> None:
    manager, store, vector, parents, retriever, db_path = _build()
    manager.ingest(_request(job_id="j1", version="v1", content=SAMPLE_MARKDOWN))
    assert _retrieve_versions(retriever) == ["v1"]

    class PublishFailingJob(IngestionJob):
        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            self._publish_attempts = 0

        def _step_publish(self) -> None:
            self._publish_attempts += 1
            if self._publish_attempts == 1:
                raise RuntimeError("simulated publish failure")
            return super()._step_publish()

    req2 = _request(job_id="j2", version="v2", content=SAMPLE_MARKDOWN + "\n\n## Extra\n")
    PublishFailingJob(
        store=store,
        vector_store=vector,
        parent_store=parents,
        chunker=ParentChildChunker(),
        dense_encoder=FakeDenseEncoder(),
        sparse_encoder=FakeSparseEncoder(),
        request=req2,
    ).run()

    # Commit already switched the active version in the control plane...
    assert store.get_active_document("t1", "eng", "doc1").version == "v2"
    old = store.get_document("t1", "eng", "doc1", "v1")
    assert old.status.value == "deprecated"
    # ...but publish failed. Control plane active=v2, so the gate makes the
    # deprecated v1 invisible; v2 points are present but still 'processing' (no
    # parent published) and are denied. Result: nothing retrievable.
    assert _retrieve_versions(retriever) == []
    assert store.get_job_status("j2") == JobStatus.FAILED
    # P1-2: the version this commit replaced must be PRESERVED after the
    # post-commit failure (not cleared), so recovery can still deprecate the old
    # data plane.
    assert store.get_job_previous_version("j2") == "v1"

    # Resume (plain job): publish succeeds, new version becomes visible, old is
    # made invisible. The already-active new version is NOT rolled back.
    manager.ingest(req2)
    assert store.get_job_status("j2") == JobStatus.SUCCEEDED
    assert store.get_active_document("t1", "eng", "doc1").version == "v2"
    assert _retrieve_versions(retriever) == ["v2"]
    # The replaced v1 data plane was actually cleaned up on recovery.
    v1_parents = [c for c in parents._store.values() if c.document_version == "v1"]
    assert v1_parents
    assert all(c.metadata.get("deprecated") for c in v1_parents)
    store.close()
    os.unlink(db_path)


def test_duplicate_job_delivery_is_idempotent() -> None:
    manager, store, vector, parents, retriever, db_path = _build()
    req = _request(job_id="j1", version="v1", content=SAMPLE_MARKDOWN)

    first = manager.ingest(req)
    assert first.status == IngestionStatus.INDEXED
    points_after_first = vector._client.count("eng").count
    chunks_after_first = len(store.list_chunk_records("t1", "eng", "doc1", "v1"))

    # Re-deliver the exact same job.
    second = manager.ingest(req)
    assert second.status == IngestionStatus.ALREADY_INDEXED

    # Exactly one active version, no duplicate points / chunk records.
    assert store.get_active_document("t1", "eng", "doc1").version == "v1"
    assert vector._client.count("eng").count == points_after_first
    assert len(store.list_chunk_records("t1", "eng", "doc1", "v1")) == chunks_after_first
    assert store.get_job_status("j1") == JobStatus.SUCCEEDED
    store.close()
    os.unlink(db_path)


def test_older_job_loses_active_version_race() -> None:
    # P1-3 + gate: an older job that acquired before a newer job committed must
    # not overwrite the now-active version when it finally commits.
    manager, store, vector, parents, retriever, db_path = _build()
    manager.ingest(_request(job_id="j0", version="v1", content=SAMPLE_MARKDOWN))
    assert _retrieve_versions(retriever) == ["v1"]

    # Job A (v2) acquires at current revision 1, then crashes before commit.
    IngestionJob(
        store=store,
        vector_store=vector,
        parent_store=parents,
        chunker=ParentChildChunker(),
        dense_encoder=FakeDenseEncoder(),
        sparse_encoder=FakeSparseEncoder(),
        request=_request(job_id="jA", version="v2", content=SAMPLE_MARKDOWN + "\n\n## A\n"),
    ).run(max_step="verify")
    # Job B (v3) acquires and commits first.
    manager.ingest(_request(job_id="jB", version="v3", content=SAMPLE_MARKDOWN + "\n\n## B\n"))
    assert store.get_active_document("t1", "eng", "doc1").version == "v3"
    assert _retrieve_versions(retriever) == ["v3"]

    # Job A finally commits using its persisted base_revision (1). Current is 2,
    # so CAS rejects -> A fails and compensates, leaving v3 active and visible.
    res_a = IngestionJob(
        store=store,
        vector_store=vector,
        parent_store=parents,
        chunker=ParentChildChunker(),
        dense_encoder=FakeDenseEncoder(),
        sparse_encoder=FakeSparseEncoder(),
        request=_request(job_id="jA", version="v2", content=SAMPLE_MARKDOWN + "\n\n## A\n"),
    ).run()
    assert res_a.status == IngestionStatus.FAILED
    assert res_a.error_code == "active_version_conflict"
    assert store.get_active_document("t1", "eng", "doc1").version == "v3"
    assert _retrieve_versions(retriever) == ["v3"]
    store.close()
    os.unlink(db_path)


def test_job_id_identity_is_immutable() -> None:
    # P1-6: a job_id bound to (tenant, corpus, document, version) cannot be
    # reused to ingest a different version.
    manager, store, vector, parents, retriever, db_path = _build()
    manager.ingest(_request(job_id="j1", version="v1", content=SAMPLE_MARKDOWN))

    import pytest

    with pytest.raises(JobIdentityConflict):
        manager.ingest(_request(job_id="j1", version="v2", content=SAMPLE_MARKDOWN))
    # Original binding unaffected.
    assert store.get_active_document("t1", "eng", "doc1").version == "v1"
    store.close()
    os.unlink(db_path)


def test_taken_over_build_cannot_corrupt_active_version() -> None:
    # E-008.3 P1-2 full-pipeline regression: when a failed build's lease is
    # taken over by a concurrent delivery that completes successfully, the stale
    # original owner is fenced out (BuildConflict) and can neither compensate
    # the winner's data plane nor corrupt the version visible to retrieval.
    manager, store, vector, parents, retriever, db_path = _build()
    req1 = _request(job_id="j1", version="v1", content=SAMPLE_MARKDOWN)

    # j1 claims the lease and writes its data plane, then crashes (failed).
    IngestionJob(
        store=store,
        vector_store=vector,
        parent_store=parents,
        chunker=ParentChildChunker(),
        dense_encoder=FakeDenseEncoder(),
        sparse_encoder=FakeSparseEncoder(),
        request=req1,
    ).run(max_step="write_qdrant")
    store.mark_job_terminal("j1", JobStatus.FAILED)

    # j2 takes over the lease and is IN-FLIGHT (running) with its data written.
    IngestionJob(
        store=store,
        vector_store=vector,
        parent_store=parents,
        chunker=ParentChildChunker(),
        dense_encoder=FakeDenseEncoder(),
        sparse_encoder=FakeSparseEncoder(),
        request=_request(job_id="j2", version="v1", content=SAMPLE_MARKDOWN),
    ).run(max_step="write_qdrant")
    assert store.get_build_owner("t1", "eng", "doc1", "v1") == "j2"
    assert store.get_job_status("j2") == JobStatus.RUNNING

    # Stale owner j1 retries while j2 is in-flight: must be fenced out.
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

    # j2 completes; the active version is correct and uncorrupted.
    res2 = manager.ingest(_request(job_id="j2", version="v1", content=SAMPLE_MARKDOWN))
    assert res2.status == IngestionStatus.INDEXED
    assert store.get_build_owner("t1", "eng", "doc1", "v1") == "j2"
    assert _retrieve_versions(retriever) == ["v1"]
    store.close()
    os.unlink(db_path)
