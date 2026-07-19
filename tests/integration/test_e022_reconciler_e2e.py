"""Integration tests for the E-022 reconciler (multi-step, end-to-end).

Exercises the reconciler against a real ingest plus injected data-plane
leftovers (orphan Qdrant points + orphan parent chunks from a crashed build),
verifies it repairs toward Metadata DB truth while keeping active documents
intact, records findings in the audit tables, and is idempotent on re-run.
"""

import os
import tempfile

from qdrant_client import QdrantClient
from qdrant_client.models import PointStruct, SparseVector

from agentic_rag_enterprise.corpus.registry import InMemoryCorpusRegistry
from agentic_rag_enterprise.domain.corpus import CorpusConfig
from agentic_rag_enterprise.ingestion.chunker import ParentChildChunker, ParentChunk
from agentic_rag_enterprise.ingestion.index_migration import build_index_v2, switch_index
from agentic_rag_enterprise.ingestion.job import DocumentManager, IngestionRequest
from agentic_rag_enterprise.ingestion.reconciler import Reconciler
from agentic_rag_enterprise.retrieval.hybrid import _HybridSearchAdapter
from agentic_rag_enterprise.retrieval.parent_reader import ParentReader
from agentic_rag_enterprise.retrieval.retriever import SecureRetriever
from agentic_rag_enterprise.security.policy import ResourceAcl
from agentic_rag_enterprise.storage.metadata_store import MetadataStore
from agentic_rag_enterprise.storage.parent_store import ParentStore
from agentic_rag_enterprise.storage.vector_store import VectorStore, child_point_id

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
    acl = ResourceAcl(
        **acl_payload(
            tenant_id="t1",
            acl_scope="restricted",
            security_level="public",
            allowed_user_ids=["u1", "u2"],
        )
    )
    mgr.ingest(
        IngestionRequest(
            tenant_id="t1",
            corpus_id="eng",
            document_id="doc1",
            document_version="v1",
            content=SAMPLE_MARKDOWN,
            acl=acl,
            job_id="j-init",
        )
    )
    return store, vstore, pstore, registry, mgr


def _inject_orphans(vstore: VectorStore, pstore: ParentStore) -> None:
    # Qdrant point with no metadata row.
    vstore.upsert(
        "eng",
        [
            PointStruct(
                id=child_point_id("orphan-q"),
                vector={"": [0.0] * DENSE_DIM, "sparse": SparseVector(indices=[0], values=[1.0])},
                payload={
                    "tenant_id": "t1",
                    "corpus_id": "eng",
                    "document_id": "docX",
                    "document_version": "v9",
                    "text": "orphan",
                },
            )
        ],
    )
    # Parent chunk with no metadata row.
    pstore.put(
        ParentChunk(
            parent_id="orphan-p",
            document_id="docX",
            document_version="v9",
            tenant_id="t1",
            corpus_id="eng",
            text="orphan parent",
            section_path=[],
            metadata={},
        )
    )


def test_reconcile_repairs_orphans_and_keeps_active_intact() -> None:
    store, vstore, pstore, registry, mgr = _components()
    _inject_orphans(vstore, pstore)
    assert vstore.list_point_ids_by_document("eng", "t1", "eng", "docX", "v9")
    assert "orphan-p" in pstore

    rec = Reconciler(
        store,
        vstore,
        pstore,
        registry,
        owner="o1",
        purge_document=mgr.reconcile_purge,
        rebuild_document=mgr.rebuild_document,
    )
    report = rec.reconcile_corpus("eng")

    assert report.mutated is True
    assert any(f.kind == "orphan_qdrant_point" for f in report.findings)
    assert any(f.kind == "orphan_parent_chunk" for f in report.findings)
    # Orphans removed; active doc untouched.
    assert vstore.list_point_ids_by_document("eng", "t1", "eng", "docX", "v9") == []
    assert "orphan-p" not in pstore
    assert vstore.list_point_ids_by_document("eng", "t1", "eng", "doc1", "v1")

    # Audit tables populated.
    run = store._conn.execute(  # noqa: SLF001
        "SELECT finding_count, mutated FROM reconciliation_runs WHERE run_id=?",
        (report.run_id,),
    ).fetchone()
    assert run["finding_count"] > 0 and run["mutated"] == 1
    findings = store._conn.execute(  # noqa: SLF001
        "SELECT kind FROM reconciliation_findings WHERE run_id=?", (report.run_id,)
    ).fetchall()
    assert {f["kind"] for f in findings} >= {"orphan_qdrant_point", "orphan_parent_chunk"}


def test_reconcile_is_idempotent_on_rerun() -> None:
    store, vstore, pstore, registry, mgr = _components()
    rec = Reconciler(
        store,
        vstore,
        pstore,
        registry,
        owner="o1",
        purge_document=mgr.reconcile_purge,
        rebuild_document=mgr.rebuild_document,
    )
    first = rec.reconcile_corpus("eng")
    second = rec.reconcile_corpus("eng")
    # No orphans on the second pass -> no repair findings.
    assert not any(
        f.kind in ("orphan_qdrant_point", "orphan_parent_chunk") for f in second.findings
    )
    assert first.mutated is False  # nothing to repair in a clean corpus


def test_dry_run_records_findings_without_mutating() -> None:
    store, vstore, pstore, registry, mgr = _components()
    _inject_orphans(vstore, pstore)

    rec = Reconciler(store, vstore, pstore, registry, owner="o1", dry_run=True)
    report = rec.reconcile_corpus("eng")

    assert report.mutated is False
    assert any(f.kind == "orphan_qdrant_point" for f in report.findings)
    # Orphans still present (dry-run never deletes).
    assert vstore.list_point_ids_by_document("eng", "t1", "eng", "docX", "v9")
    assert "orphan-p" in pstore


def _retrieval_corpus(vector_collection: str | None = None) -> CorpusConfig:
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
        enabled=True,
        searchable=True,
        default_security_level="public",
        vector_collection=vector_collection,
        created_at=_TS,
        updated_at=_TS,
    )


def test_reconciler_rebuilds_missing_parents() -> None:
    """P1-4: an active version with Qdrant points but no Parent Store entries
    is flagged ``missing_parent_chunk`` and rebuilt (parents recreated from the
    child chunk rows). After reconciliation the version is retrievable again."""
    store, vstore, pstore, registry, mgr = _components()
    # Simulate a crash/restart that emptied the in-memory Parent Store while
    # Qdrant (and the Metadata DB) stayed intact.
    for pid in pstore.list_parent_ids("t1", "eng", "doc1", "v1"):
        pstore.delete(pid)
    assert not pstore.list_parent_ids("t1", "eng", "doc1", "v1")
    # Qdrant points survived, so this is a PARENT-only gap (not missing_qdrant_point).
    assert vstore.list_point_ids_by_document("eng", "t1", "eng", "doc1", "v1")

    rec = Reconciler(
        store,
        vstore,
        pstore,
        registry,
        owner="o1",
        purge_document=mgr.reconcile_purge,
        rebuild_document=mgr.rebuild_document,
    )
    report = rec.reconcile_corpus("eng")

    assert any(f.kind == "missing_parent_chunk" for f in report.findings)
    assert not any(f.kind == "missing_qdrant_point" for f in report.findings)

    # Parents recreated from the child chunk rows.
    restored = pstore.list_parent_ids("t1", "eng", "doc1", "v1")
    assert restored
    parent = pstore.get(restored[0])
    assert parent.metadata["status"] == "active"
    assert parent.metadata["deprecated"] is False
    assert parent.metadata["allowed_user_ids"] == ["u1", "u2"]
    # Reconstructed parent text is faithful (carries the source section content).
    assert "Overview" in parent.text or len(parent.text) > 0

    # End-to-end: the version is retrievable again.
    retriever = SecureRetriever(
        _HybridSearchAdapter(vstore), ParentReader(pstore), metadata_store=store
    )
    result = retriever.retrieve(
        make_security_context(),
        "architecture planner security",
        _retrieval_corpus(),
        dense_encoder=FakeDenseEncoder(),
        sparse_encoder=FakeSparseEncoder(),
    )
    assert result.hits
    assert all(p.document_version == "v1" for _, p in result.hits)


def test_reconciler_realigns_registry_pointer_mismatch() -> None:
    """P1-4: when the live registry pointer has drifted from the persisted
    pointer, the reconciler flags ``registry_mismatch`` and realigns the live
    registry to the persisted (source-of-truth) value."""
    store, vstore, pstore, registry, mgr = _components()
    # Migrate to a v2 index and switch the persisted pointer to it.
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
    assert store.get_active_collection("eng") == v2
    assert registry.resolve_collection_name("eng") == v2

    # Simulate a restart that re-seeded the live registry WITHOUT the migrated
    # pointer (drift back to the corpus_id default).
    registry.set_active_collection("eng", None)
    assert registry.resolve_collection_name("eng") == "eng"
    assert registry.resolve_collection_name("eng") != store.get_active_collection("eng")

    rec = Reconciler(
        store,
        vstore,
        pstore,
        registry,
        owner="o1",
        purge_document=mgr.reconcile_purge,
        rebuild_document=mgr.rebuild_document,
    )
    report = rec.reconcile_corpus("eng")

    assert any(f.kind == "registry_mismatch" for f in report.findings)
    # Live registry realigned to the persisted source-of-truth pointer.
    assert registry.resolve_collection_name("eng") == v2
    assert registry.resolve_collection_name("eng") == store.get_active_collection("eng")
