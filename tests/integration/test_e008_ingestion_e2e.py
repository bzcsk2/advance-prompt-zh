"""End-to-end E-008: idempotent ingest -> active-version switch -> retrieve.

Builds the full control-plane + data-plane chain (MetadataStore -> ParentStore
-> Qdrant -> SecureRetriever) and asserts the behavior the build plan requires:
an ingested version is retrievable, a content update switches the active version
without breaking the old one, and the tenant/corpus identity chain is preserved.
Hermetic: temp metadata DB, in-memory Qdrant, fake encoders.
"""

import os
import tempfile
from datetime import datetime

from qdrant_client import QdrantClient

from agentic_rag_enterprise.domain.corpus import CorpusConfig
from agentic_rag_enterprise.ingestion.chunker import ParentChildChunker
from agentic_rag_enterprise.ingestion.job import DocumentManager, IngestionRequest
from agentic_rag_enterprise.retrieval.hybrid import _HybridSearchAdapter
from agentic_rag_enterprise.retrieval.parent_reader import ParentReader
from agentic_rag_enterprise.retrieval.retriever import SecureRetriever
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


def _build() -> (
    tuple[DocumentManager, MetadataStore, VectorStore, ParentStore, SecureRetriever, str]
):
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


def test_ingest_then_retrieve_closed_loop() -> None:
    manager, store, vector, parents, retriever, db_path = _build()
    res = manager.ingest(_request(job_id="j1", version="v1", content=SAMPLE_MARKDOWN))
    assert res.status.value == "indexed"

    result = retriever.retrieve(
        make_security_context(),
        "architecture planner security",
        _corpus(),
        dense_encoder=FakeDenseEncoder(),
        sparse_encoder=FakeSparseEncoder(),
    )
    assert result.hits
    assert result.denied_parent_count == 0
    for hit, parent in result.hits:
        assert parent.document_version == "v1"
        assert parent.tenant_id == "t1"
        assert parent.corpus_id == "eng"
    store.close()
    os.unlink(db_path)


def test_content_update_switches_active_version() -> None:
    manager, store, vector, parents, retriever, db_path = _build()
    manager.ingest(_request(job_id="j1", version="v1", content=SAMPLE_MARKDOWN))
    # Content change -> new version.
    updated = SAMPLE_MARKDOWN + "\n\n## New Section\n\nUnique update marker token.\n"
    manager.ingest(_request(job_id="j2", version="v2", content=updated))

    active = store.get_active_document("t1", "eng", "doc1")
    assert active.version == "v2"
    old = store.get_document("t1", "eng", "doc1", "v1")
    assert old.status.value == "deprecated"

    result = retriever.retrieve(
        make_security_context(),
        "Unique update marker token",
        _corpus(),
        dense_encoder=FakeDenseEncoder(),
        sparse_encoder=FakeSparseEncoder(),
    )
    # New version content is retrievable.
    assert any("Unique update marker token" in p.content for _, p in result.hits)
    # Old (deprecated) version is NOT returned.
    assert all(p.document_version == "v2" for _, p in result.hits)
    store.close()
    os.unlink(db_path)


def test_old_active_version_remains_until_new_commits() -> None:
    manager, store, vector, parents, retriever, db_path = _build()
    manager.ingest(_request(job_id="j1", version="v1", content=SAMPLE_MARKDOWN))
    # Crash the new version before commit: it must not disturb the active v1.
    from agentic_rag_enterprise.ingestion.job import IngestionJob

    req = _request(job_id="j2", version="v2", content=SAMPLE_MARKDOWN + "\n\n## Extra\n")
    IngestionJob(
        store=store,
        vector_store=vector,
        parent_store=parents,
        chunker=ParentChildChunker(),
        dense_encoder=FakeDenseEncoder(),
        sparse_encoder=FakeSparseEncoder(),
        request=req,
    ).run(max_step="write_parents")

    active = store.get_active_document("t1", "eng", "doc1")
    assert active.version == "v1"  # v1 still the only active version
    result = retriever.retrieve(
        make_security_context(),
        "architecture planner security",
        _corpus(),
        dense_encoder=FakeDenseEncoder(),
        sparse_encoder=FakeSparseEncoder(),
    )
    assert all(p.document_version == "v1" for _, p in result.hits)
    store.close()
    os.unlink(db_path)


def test_tenant_binding_is_enforced_on_retrieval() -> None:
    manager, store, vector, parents, retriever, db_path = _build()
    manager.ingest(_request(job_id="j1", version="v1", content=SAMPLE_MARKDOWN))
    # A different tenant cannot see t1's document (corpus gate + ACL filter).
    result = retriever.retrieve(
        make_security_context(tenant_id="t2"),
        "anything",
        _corpus(),
        dense_encoder=FakeDenseEncoder(),
        sparse_encoder=FakeSparseEncoder(),
    )
    assert result.hits == []
    store.close()
    os.unlink(db_path)


def test_control_plane_active_version_gate_filters_inactive_points() -> None:
    # P1-1: a query whose terms appear in BOTH v1 and v2 must return only the
    # ACTIVE version's hits. v1 points are physically retained (deprecated, not
    # deleted), so only the control-plane gate keeps them out of results.
    manager, store, vector, parents, retriever, db_path = _build()
    manager.ingest(_request(job_id="j1", version="v1", content=SAMPLE_MARKDOWN))
    manager.ingest(_request(job_id="j2", version="v2", content=SAMPLE_MARKDOWN))
    assert store.get_active_document("t1", "eng", "doc1").version == "v2"
    # Both versions physically present in the vector store.
    assert vector._client.count("eng").count > 0

    result = retriever.retrieve(
        make_security_context(),
        "architecture planner security",  # token present in both versions
        _corpus(),
        dense_encoder=FakeDenseEncoder(),
        sparse_encoder=FakeSparseEncoder(),
    )
    assert result.hits
    assert all(p.document_version == "v2" for _, p in result.hits)
    store.close()
    os.unlink(db_path)
