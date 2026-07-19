"""Unit tests for E-022 index migration + rollback.

Hermetic. Verifies v2 is built *alongside* v1, the retrieval pointer flips
atomically and retains v1, rollback restores v1 (kept, not deleted), and a
switch to a missing collection is refused.
"""

import os
import tempfile

from qdrant_client import QdrantClient

from agentic_rag_enterprise.corpus.registry import InMemoryCorpusRegistry
from agentic_rag_enterprise.domain.corpus import CorpusConfig
from agentic_rag_enterprise.ingestion.chunker import ParentChildChunker
from agentic_rag_enterprise.ingestion.index_migration import (
    build_index_v2,
    new_collection_name,
    rollback_index,
    switch_index,
)
from agentic_rag_enterprise.ingestion.job import DocumentManager, IngestionRequest
from agentic_rag_enterprise.security.policy import ResourceAcl
from agentic_rag_enterprise.storage.metadata_store import MetadataStore
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

from datetime import datetime, timezone

_TS = datetime(2024, 1, 1, tzinfo=timezone.utc)


def _corpus_config() -> CorpusConfig:
    return CorpusConfig(
        corpus_id="eng",
        tenant_id="t1",
        name="eng",
        description="",
        domain="wiki",
        owner="o",
        source_type="wiki",
        capability_ids=["retrieval"],
        security_policy_id="default",
        created_at=_TS,
        updated_at=_TS,
    )


def _components():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    store = MetadataStore(path)
    store._conn.execute(  # noqa: SLF001
        "INSERT INTO corpus_registry "
        "(corpus_id, tenant_id, name, description, created_at, updated_at) "
        "VALUES (?, ?, 'corpus', '', ?, ?)",
        ("eng", "t1", "2024-01-01T00:00:00+00:00", "2024-01-01T00:00:00+00:00"),
    )
    client = QdrantClient(location=":memory:")
    vstore = VectorStore(client)
    vstore.create_collection("eng", dense_size=DENSE_DIM)
    pstore = ParentStore()
    registry = InMemoryCorpusRegistry([_corpus_config()])
    mgr = DocumentManager(
        metadata_store=store,
        vector_store=vstore,
        parent_store=pstore,
        chunker=ParentChildChunker(),
        dense_encoder=FakeDenseEncoder(),
        sparse_encoder=FakeSparseEncoder(),
        corpus_registry=registry,
    )
    mgr.ingest(
        IngestionRequest(
            tenant_id="t1",
            corpus_id="eng",
            document_id="doc1",
            document_version="v1",
            content=SAMPLE_MARKDOWN,
            acl=ResourceAcl(
                **acl_payload(
                    tenant_id="t1",
                    acl_scope="restricted",
                    security_level="public",
                    allowed_user_ids=["u1", "u2"],
                )
            ),
            job_id="j-init",
        )
    )
    return store, vstore, pstore, registry, mgr


def test_build_creates_parallel_collection_retaining_v1() -> None:
    store, vstore, pstore, registry, _ = _components()
    v1_points = len(vstore.list_point_ids_by_document("eng", "t1", "eng", "doc1", "v1"))
    assert v1_points > 0

    v2 = build_index_v2(
        "eng",
        embedding_version="v2",
        chunking_version="v1",
        dense_size=DENSE_DIM,
        metadata_store=store,
        vector_store=vstore,
        corpus_registry=registry,
        dense_encoder=FakeDenseEncoder(),
        sparse_encoder=FakeSparseEncoder(),
    )
    assert v2 == new_collection_name("eng", embedding_version="v2", chunking_version="v1")
    # v2 exists with the same point count; v1 is untouched.
    assert vstore.collection_exists(v2)
    v2_points = vstore.list_point_ids_by_document(v2, "t1", "eng", "doc1", "v1")
    assert len(v2_points) == v1_points
    # Pointer has NOT switched yet (build is separate from switch).
    assert registry.resolve_collection_name("eng") == "eng"


def test_switch_flips_pointer_and_rollback_restores_v1() -> None:
    store, vstore, pstore, registry, _ = _components()
    v2 = build_index_v2(
        "eng",
        embedding_version="v2",
        chunking_version="v1",
        dense_size=DENSE_DIM,
        metadata_store=store,
        vector_store=vstore,
        corpus_registry=registry,
        dense_encoder=FakeDenseEncoder(),
        sparse_encoder=FakeSparseEncoder(),
    )
    switch_index(
        "eng",
        target_collection=v2,
        metadata_store=store,
        corpus_registry=registry,
        vector_store=vstore,
    )
    assert registry.resolve_collection_name("eng") == v2
    assert store.get_active_collection("eng") == v2

    # Roll back: pointer returns to v1, which is retained (not deleted).
    previous = rollback_index(
        "eng", metadata_store=store, corpus_registry=registry, vector_store=vstore
    )
    assert previous == "eng"
    assert registry.resolve_collection_name("eng") == "eng"
    assert vstore.collection_exists(v2)  # retained for later purge


def test_switch_refuses_missing_collection() -> None:
    store, vstore, pstore, registry, _ = _components()
    try:
        switch_index(
            "eng",
            target_collection="eng_ghost",
            metadata_store=store,
            corpus_registry=registry,
            vector_store=vstore,
        )
        raise AssertionError("expected ValueError")
    except ValueError:
        pass
    assert registry.resolve_collection_name("eng") == "eng"


def test_rollback_without_prior_build_raises() -> None:
    store, vstore, pstore, registry, _ = _components()
    try:
        rollback_index("eng", metadata_store=store, corpus_registry=registry, vector_store=vstore)
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_dry_run_switch_does_not_mutate() -> None:
    store, vstore, pstore, registry, _ = _components()
    v2 = build_index_v2(
        "eng",
        embedding_version="v2",
        chunking_version="v1",
        dense_size=DENSE_DIM,
        metadata_store=store,
        vector_store=vstore,
        corpus_registry=registry,
        dense_encoder=FakeDenseEncoder(),
        sparse_encoder=FakeSparseEncoder(),
    )
    switch_index(
        "eng",
        target_collection=v2,
        metadata_store=store,
        corpus_registry=registry,
        vector_store=vstore,
        dry_run=True,
    )
    assert registry.resolve_collection_name("eng") == "eng"


def test_build_excludes_deprecated_version_after_update() -> None:
    """P1-1: a v2 migration must NOT resurrect the deprecated prior version."""
    store, vstore, pstore, registry, mgr = _components()
    mgr.ingest(
        IngestionRequest(
            tenant_id="t1",
            corpus_id="eng",
            document_id="doc1",
            document_version="v1",
            content=SAMPLE_MARKDOWN,
            acl=ResourceAcl(
                **acl_payload(tenant_id="t1", acl_scope="restricted", security_level="public")
            ),
            job_id="j-v1",
        )
    )
    mgr.ingest(
        IngestionRequest(
            tenant_id="t1",
            corpus_id="eng",
            document_id="doc1",
            document_version="v2",
            content="# Updated\n\nDifferent body text for v2.",
            acl=ResourceAcl(
                **acl_payload(tenant_id="t1", acl_scope="restricted", security_level="public")
            ),
            job_id="j-v2",
        )
    )
    # v1 is now deprecated; only v2 is active.
    assert store.get_active_version("t1", "eng", "doc1") == "v2"

    v2 = build_index_v2(
        "eng",
        embedding_version="v2",
        chunking_version="v1",
        dense_size=DENSE_DIM,
        metadata_store=store,
        vector_store=vstore,
        corpus_registry=registry,
        dense_encoder=FakeDenseEncoder(),
        sparse_encoder=FakeSparseEncoder(),
    )
    # Migrated collection carries the active v2, NOT the deprecated v1.
    assert vstore.list_point_ids_by_document(v2, "t1", "eng", "doc1", "v2")
    assert vstore.list_point_ids_by_document(v2, "t1", "eng", "doc1", "v1") == []


def test_build_excludes_logically_deleted_document() -> None:
    """P1-1: a logically-deleted document must not appear in the migrated index."""
    store, vstore, pstore, registry, mgr = _components()
    mgr.ingest(
        IngestionRequest(
            tenant_id="t1",
            corpus_id="eng",
            document_id="doc1",
            document_version="v1",
            content=SAMPLE_MARKDOWN,
            acl=ResourceAcl(
                **acl_payload(tenant_id="t1", acl_scope="restricted", security_level="public")
            ),
            job_id="j-v1",
        )
    )
    mgr.delete(make_security_context(), corpus_id="eng", document_id="doc1")  # status=deleted

    v2 = build_index_v2(
        "eng",
        embedding_version="v2",
        chunking_version="v1",
        dense_size=DENSE_DIM,
        metadata_store=store,
        vector_store=vstore,
        corpus_registry=registry,
        dense_encoder=FakeDenseEncoder(),
        sparse_encoder=FakeSparseEncoder(),
    )
    # Deleted evidence is never re-indexed.
    assert vstore.list_point_ids_by_document(v2, "t1", "eng", "doc1", "v1") == []
