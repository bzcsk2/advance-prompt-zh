"""Integration tests for E-022 index migration + active-version rollback.

Exercises the full build -> switch -> rollback cycle against a real ingest, and
the active-version rollback (build plan §2630) against real version rows. Verifies
the retrieval pointer (CorpusConfig.vector_collection) flips atomically, v1 is
retained (never cleared-and-rebuilt), and a rolled-back version is still
retrievable while the superseded version is deprecated.

Hermetic: in-memory SQLite + in-memory Qdrant + Fake encoders.
"""

import os
import tempfile

import pytest
from qdrant_client import QdrantClient

from agentic_rag_enterprise.corpus.registry import InMemoryCorpusRegistry
from agentic_rag_enterprise.domain.corpus import CorpusConfig
from agentic_rag_enterprise.ingestion.chunker import ParentChildChunker
from agentic_rag_enterprise.ingestion.index_migration import (
    IndexSwitchConflict,
    build_index_v2,
    new_collection_name,
    rollback_index,
    switch_index,
)
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
                **acl_payload(tenant_id="t1", acl_scope="restricted", security_level="public")
            ),
            job_id="j-init",
        )
    )
    return store, vstore, pstore, registry, mgr


def test_build_then_switch_keeps_v1_and_retrieval_points_to_v2() -> None:
    store, vstore, pstore, registry, mgr = _components()
    v1_points = vstore.list_point_ids_by_document("eng", "t1", "eng", "doc1", "v1")
    assert v1_points  # sanity: ingest populated the live collection

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
    # v2 built alongside v1; both carry the same points.
    assert vstore.collection_exists(v2)
    assert len(vstore.list_point_ids_by_document(v2, "t1", "eng", "doc1", "v1")) == len(v1_points)
    assert registry.resolve_collection_name("eng") == "eng"  # build != switch

    switch_index(
        "eng",
        target_collection=v2,
        metadata_store=store,
        corpus_registry=registry,
        vector_store=vstore,
    )
    # After switch, retrieval pointer (and persisted registry) both point at v2.
    assert registry.resolve_collection_name("eng") == v2
    assert store.get_active_collection("eng") == v2
    # v2 is now the collection the pointer resolves to; its points are intact.
    assert vstore.list_point_ids_by_document(v2, "t1", "eng", "doc1", "v1") == v1_points
    # v1 is retained, not deleted.
    assert vstore.collection_exists("eng")


def test_rollback_index_restores_v1_and_retains_v2() -> None:
    store, vstore, pstore, registry, mgr = _components()
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

    previous = rollback_index(
        "eng", metadata_store=store, corpus_registry=registry, vector_store=vstore
    )
    assert previous == "eng"  # pointer returns to v1
    assert registry.resolve_collection_name("eng") == "eng"
    assert store.get_active_collection("eng") == "eng"
    # v1 retrieval still serves the doc after rollback.
    assert vstore.list_point_ids_by_document("eng", "t1", "eng", "doc1", "v1")
    # v2 is retained (superseded, never cleared-and-rebuilt) for later purge.
    assert vstore.collection_exists(v2)


def test_rollback_active_version_returns_previous_and_deprecates_newer() -> None:
    store, vstore, pstore, registry, mgr = _components()
    # Publish a newer version; v1 becomes deprecated, v2 active.
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
    assert store.get_active_version("t1", "eng", "doc1") == "v2"

    new_rev, prior = mgr.rollback_active_version("t1", "eng", "doc1")
    assert prior == "v2"
    assert isinstance(new_rev, int)
    # v1 is active again; v2 is deprecated (not deleted / not resurrected-from-deleted).
    assert store.get_active_version("t1", "eng", "doc1") == "v1"
    v2_row = store.get_document("t1", "eng", "doc1", "v2")
    assert v2_row is not None and v2_row.status.value == "deprecated"


def test_rollback_active_version_is_idempotent() -> None:
    store, vstore, pstore, registry, mgr = _components()
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
    rev1, _ = mgr.rollback_active_version("t1", "eng", "doc1")
    # Re-asserting the same (already-active) target is a no-op (no revision bump).
    rev2, _ = mgr.rollback_active_version("t1", "eng", "doc1", to_version="v1")
    assert rev2 == rev1
    assert store.get_active_version("t1", "eng", "doc1") == "v1"


# --------------------------------------------------------------------------- #
# P1-1 / P1-2: real-retriever + data-plane evidence for active-version rollback
# --------------------------------------------------------------------------- #
def _retrieval_corpus(vector_collection: str | None = None) -> CorpusConfig:
    """A discoverable corpus config suitable for SecureRetriever.validate_corpus."""
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


def _ingest(mgr: DocumentManager, *, doc_id: str, version: str, content: str) -> None:
    mgr.ingest(
        IngestionRequest(
            tenant_id="t1",
            corpus_id="eng",
            document_id=doc_id,
            document_version=version,
            content=content,
            acl=ResourceAcl(
                **acl_payload(tenant_id="t1", acl_scope="tenant", security_level="public")
            ),
            job_id=f"j-{doc_id}-{version}",
        )
    )


def _secure_retriever(
    vstore: VectorStore, pstore: ParentStore, store: MetadataStore
) -> SecureRetriever:
    return SecureRetriever(_HybridSearchAdapter(vstore), ParentReader(pstore), metadata_store=store)


def _fresh_components():
    """Like ``_components`` but does NOT pre-ingest, so the retrieval tests can
    ingest with a tenant-scoped ACL (``make_security_context`` passes) instead of
    the restricted ACL ``_components`` uses."""
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
    return store, vstore, pstore, registry, mgr


def test_rollback_active_version_makes_rolled_back_version_retrievable() -> None:
    """P1-2: after rolling back to v1, retrieval serves v1 and NOT v2.

    Without the data-plane sync (Qdrant payload + Parent Store), v1's parent
    stays deprecated from the v2 publish and ``ParentReader``'s second-auth
    drops it — so the rolled-back version would silently vanish from retrieval
    even though Metadata DB says it is active.
    """
    store, vstore, pstore, registry, mgr = _fresh_components()
    _ingest(mgr, doc_id="doc1", version="v1", content=SAMPLE_MARKDOWN)
    _ingest(
        mgr,
        doc_id="doc1",
        version="v2",
        content="# Updated\n\nDifferent body text for v2 rollback.",
    )
    assert store.get_active_version("t1", "eng", "doc1") == "v2"

    mgr.rollback_active_version("t1", "eng", "doc1")  # -> v1
    assert store.get_active_version("t1", "eng", "doc1") == "v1"

    retriever = _secure_retriever(vstore, pstore, store)
    result = retriever.retrieve(
        make_security_context(),
        "architecture planner security",
        _retrieval_corpus(),
        dense_encoder=FakeDenseEncoder(),
        sparse_encoder=FakeSparseEncoder(),
    )
    assert result.hits, "rolled-back v1 must remain retrievable"
    versions = {hit.document_version for hit, _ in result.hits}
    assert versions == {"v1"}
    assert all(p.document_version == "v1" for _, p in result.hits)


def test_rollback_active_version_syncs_qdrant_payload() -> None:
    """P1-2: rollback forces the data plane to match the control plane.

    v1 points must read active/non-deprecated; v2 points must read
    deprecated. This is what lets retrieval's ``status==active AND
    deprecated==false`` gate serve exactly the control-plane active version.
    """
    store, vstore, pstore, registry, mgr = _fresh_components()
    _ingest(mgr, doc_id="doc1", version="v1", content=SAMPLE_MARKDOWN)
    _ingest(
        mgr,
        doc_id="doc1",
        version="v2",
        content="# Updated\n\nDifferent body text for v2 rollback.",
    )
    mgr.rollback_active_version("t1", "eng", "doc1")

    v1_points = vstore.list_point_ids_by_document("eng", "t1", "eng", "doc1", "v1")
    v2_points = vstore.list_point_ids_by_document("eng", "t1", "eng", "doc1", "v2")
    assert v1_points and v2_points
    payloads = dict(vstore.scroll_all("eng"))
    v1_payload = payloads[v1_points[0]]
    v2_payload = payloads[v2_points[0]]
    assert v1_payload["status"] == "active" and v1_payload["deprecated"] is False
    assert v2_payload["status"] == "deprecated" and v2_payload["deprecated"] is True

    # Parent Store must agree: v1 parents active, v2 parents deprecated.
    v1_parents = pstore.list_parent_ids("t1", "eng", "doc1", "v1")
    v2_parents = pstore.list_parent_ids("t1", "eng", "doc1", "v2")
    assert v1_parents and v2_parents
    assert pstore.get(v1_parents[0]).metadata["deprecated"] is False
    assert pstore.get(v2_parents[0]).metadata["deprecated"] is True


def test_rollback_active_version_records_finding_on_data_plane_failure() -> None:
    """P1-2: a data-plane sync failure must NOT silently succeed.

    The control-plane CAS already flipped v1 -> active; if the data-plane sync
    then raises, a durable, reconciler-retryable finding must be recorded and
    the error re-raised so operators see the partial result.
    """
    store, vstore, pstore, registry, mgr = _fresh_components()
    _ingest(mgr, doc_id="doc1", version="v1", content=SAMPLE_MARKDOWN)
    _ingest(
        mgr,
        doc_id="doc1",
        version="v2",
        content="# Updated\n\nDifferent body text for v2 rollback.",
    )

    # Force the data-plane sync to fail.
    original = mgr._sync_version_status

    def _boom(*args, **kwargs):  # noqa: ANN002, ANN003
        if not getattr(_boom, "armed", False):
            _boom.armed = True
            raise RuntimeError("simulated data-plane outage")
        return original(*args, **kwargs)

    _boom.armed = False  # type: ignore[attr-defined]
    mgr._sync_version_status = _boom  # type: ignore[assignment]

    raised = False
    try:
        mgr.rollback_active_version("t1", "eng", "doc1")
    except RuntimeError:
        raised = True
    assert raised, "data-plane sync failure must propagate"

    # Control plane did commit v1 active...
    assert store.get_active_version("t1", "eng", "doc1") == "v1"
    # ...but a durable finding was left for the reconciler to retry.
    rows = store._conn.execute(  # noqa: SLF001 - test reaches raw conn
        "SELECT kind, document_version, detail FROM reconciliation_findings "
        "WHERE corpus_id='eng' AND kind='rollback_data_plane_sync_failed'",
    ).fetchall()
    assert rows, "a rollback_data_plane_sync_failed finding must be persisted"
    assert rows[0]["document_version"] == "v1"
    assert "simulated data-plane outage" in rows[0]["detail"]


def test_build_and_switch_serves_only_active_version() -> None:
    """P1-1: the migrated v2 index carries only active versions, and switching
    to it serves the active version while the old version is not resurrected.

    The unit tests prove the collection is built active-only; this end-to-end
    test confirms retrieval over the switched pointer never surfaces the old
    (deprecated) version's evidence.
    """
    store, vstore, pstore, registry, mgr = _fresh_components()
    _ingest(mgr, doc_id="doc1", version="v1", content=SAMPLE_MARKDOWN)
    _ingest(
        mgr, doc_id="doc1", version="v2", content="# Updated\n\nDifferent body text for v2 switch."
    )

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
    # Data-plane isolation: the v2 collection must NOT contain v1's points.
    v1_in_v2 = vstore.list_point_ids_by_document(v2, "t1", "eng", "doc1", "v1")
    v2_in_v2 = vstore.list_point_ids_by_document(v2, "t1", "eng", "doc1", "v2")
    assert v2_in_v2 and not v1_in_v2

    switch_index(
        "eng",
        target_collection=v2,
        metadata_store=store,
        corpus_registry=registry,
        vector_store=vstore,
    )
    retriever = _secure_retriever(vstore, pstore, store)
    result = retriever.retrieve(
        make_security_context(),
        "architecture planner security",
        _retrieval_corpus(vector_collection=v2),
        dense_encoder=FakeDenseEncoder(),
        sparse_encoder=FakeSparseEncoder(),
    )
    assert result.hits
    assert {hit.document_version for hit, _ in result.hits} == {"v2"}


# --------------------------------------------------------------------------- #
# P1-3: atomic index switch + per-corpus lease
# --------------------------------------------------------------------------- #
class _FailingRegistry(InMemoryCorpusRegistry):
    """A live registry whose pointer flip always raises (fault injection)."""

    def set_active_collection(self, corpus_id: str, collection_name: str) -> None:
        raise RuntimeError("simulated live-registry outage")


def _build_v2_collection(store, vstore, registry, mgr):
    _ingest(mgr, doc_id="doc1", version="v1", content=SAMPLE_MARKDOWN)
    return build_index_v2(
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


def test_index_switch_lease_serializes_concurrent_switches() -> None:
    """At most one switch owns the per-corpus lease at a time."""
    store, vstore, pstore, registry, mgr = _fresh_components()
    assert store.acquire_index_switch_lease("eng", "owner-A") is True
    # A different owner is rejected while the lease is held.
    assert store.acquire_index_switch_lease("eng", "owner-B") is False
    # The holder can refresh its own lease.
    assert store.acquire_index_switch_lease("eng", "owner-A") is True
    # After release, another owner can take it.
    store.release_index_switch_lease("eng", "owner-A")
    assert store.acquire_index_switch_lease("eng", "owner-B") is True
    store.release_index_switch_lease("eng", "owner-B")


def test_concurrent_switch_raises_conflict() -> None:
    """A second concurrent switch must be rejected, then succeed once free."""
    store, vstore, pstore, registry, mgr = _fresh_components()
    v2 = _build_v2_collection(store, vstore, registry, mgr)

    # Owner A grabs the lease (simulating an in-flight switch).
    assert store.acquire_index_switch_lease("eng", "owner-A") is True
    with pytest.raises(IndexSwitchConflict):
        switch_index(
            "eng",
            target_collection=v2,
            metadata_store=store,
            corpus_registry=registry,
            vector_store=vstore,
            owner="owner-B",
        )
    # Once A releases, B's switch proceeds and flips both pointers.
    store.release_index_switch_lease("eng", "owner-A")
    switch_index(
        "eng",
        target_collection=v2,
        metadata_store=store,
        corpus_registry=registry,
        vector_store=vstore,
        owner="owner-B",
    )
    assert store.get_active_collection("eng") == v2
    assert registry.resolve_collection_name("eng") == v2


def test_switch_index_compensates_when_registry_update_fails() -> None:
    """If the live registry flip fails, the persisted pointer is reverted (not
    left pointing at an unreachable collection) and the lease is released."""
    store, vstore, pstore, registry, mgr = _fresh_components()
    v2 = _build_v2_collection(store, vstore, registry, mgr)
    failing = _FailingRegistry([_corpus_config()])
    previous_pointer = store.get_active_collection("eng")

    with pytest.raises(RuntimeError):
        switch_index(
            "eng",
            target_collection=v2,
            metadata_store=store,
            corpus_registry=failing,
            vector_store=vstore,
        )

    # Compensated: persisted pointer reverted to its pre-switch value, NOT left
    # pointing at the unreachable v2 collection.
    assert store.get_active_collection("eng") == previous_pointer
    assert store.get_active_collection("eng") != v2
    # Lease released: a fresh switch can now acquire it.
    assert store.acquire_index_switch_lease("eng", "owner-probe") is True
    store.release_index_switch_lease("eng", "owner-probe")
