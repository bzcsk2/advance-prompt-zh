"""Unit tests for DocumentManager mutation API (E-010).

Covers update (new active version), logical delete (3-plane flip), physical
purge (scoped + idempotent), ACL tightening (payload-only, no re-embed), and
fail-closed cross-tenant / non-discoverable refusal.
"""

import os
import tempfile

import pytest
from qdrant_client import QdrantClient

from agentic_rag_enterprise.domain.ingestion import DocumentStatus
from agentic_rag_enterprise.ingestion.chunker import ParentChildChunker
from agentic_rag_enterprise.ingestion.job import (
    DocumentManager,
    DocumentMutationError,
    IngestionRequest,
)
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


def _fresh_store():
    """Fresh MetadataStore with a corpus row but NO document (so the manager
    owns the full lifecycle, avoiding the seeded-v1 content_hash conflict)."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    store = MetadataStore(path)
    store._conn.execute(  # noqa: SLF001 - test helper reaches into raw conn
        "INSERT INTO corpus_registry "
        "(corpus_id, tenant_id, name, description, created_at, updated_at) "
        "VALUES (?, ?, 'corpus', '', ?, ?)",
        ("eng", "t1", "2024-01-01T00:00:00+00:00", "2024-01-01T00:00:00+00:00"),
    )
    return store, path


def _mgr_and_ctx():
    mstore, _path = _fresh_store()
    client = QdrantClient(location=":memory:")
    vstore = VectorStore(client)
    vstore.create_collection("eng", dense_size=DENSE_DIM)
    pstore = ParentStore()
    mgr = DocumentManager(
        metadata_store=mstore,
        vector_store=vstore,
        parent_store=pstore,
        chunker=ParentChildChunker(),
        dense_encoder=FakeDenseEncoder(),
        sparse_encoder=FakeSparseEncoder(),
    )
    return mgr, mstore, vstore, pstore, make_security_context()


def _ingest_v1(mgr: DocumentManager) -> None:
    acl = ResourceAcl(**acl_payload(tenant_id="t1", acl_scope="tenant", security_level="public"))
    req = IngestionRequest(
        tenant_id="t1", corpus_id="eng", document_id="doc1",
        document_version="v1", content=SAMPLE_MARKDOWN, acl=acl, job_id="j-init",
    )
    mgr.ingest(req)


def _acl() -> ResourceAcl:
    return ResourceAcl(**acl_payload(tenant_id="t1", acl_scope="tenant", security_level="public"))


def test_update_creates_new_active_version_and_deprecates_old() -> None:
    mgr, mstore, _, _, ctx = _mgr_and_ctx()
    _ingest_v1(mgr)
    assert mstore.get_active_version("t1", "eng", "doc1") == "v1"

    mgr.update(
        ctx, corpus_id="eng", document_id="doc1",
        content="updated content", job_id="j-upd", document_version="v2", acl=_acl(),
    )
    assert mstore.get_active_version("t1", "eng", "doc1") == "v2"
    assert mstore.get_document("t1", "eng", "doc1", "v1").status == DocumentStatus.DEPRECATED


def test_delete_logical_flips_three_planes_and_is_idempotent() -> None:
    mgr, mstore, vstore, _, ctx = _mgr_and_ctx()
    _ingest_v1(mgr)

    mgr.delete(ctx, corpus_id="eng", document_id="doc1")

    # Control plane: no active version + row marked deleted.
    assert mstore.get_active_version("t1", "eng", "doc1") is None
    assert mstore.get_document("t1", "eng", "doc1", "v1").status == DocumentStatus.DELETED
    # Data plane Qdrant points flipped (retrieval filters via status=active).
    ids = vstore.list_point_ids_by_document("eng", "t1", "eng", "doc1", "v1")
    assert ids
    retrieved = vstore._client.retrieve(collection_name="eng", ids=ids, with_payload=True)
    assert all(p.payload["status"] == "deleted" and p.payload["deprecated"] is True for p in retrieved)
    # Idempotent re-run is a no-op (no error).
    mgr.delete(ctx, corpus_id="eng", document_id="doc1")


def test_purge_removes_data_plane_and_is_idempotent() -> None:
    mgr, mstore, vstore, _, ctx = _mgr_and_ctx()
    _ingest_v1(mgr)
    mgr.delete(ctx, corpus_id="eng", document_id="doc1")

    mgr.purge(ctx, corpus_id="eng", document_id="doc1")

    assert mstore.get_document_latest("t1", "eng", "doc1") is None
    assert vstore.list_point_ids_by_document("eng", "t1", "eng", "doc1", "v1") == []
    # Idempotent: re-purge on an absent document is a no-op.
    mgr.purge(ctx, corpus_id="eng", document_id="doc1")


def test_purge_refuses_non_deleted_document() -> None:
    mgr, _, _, _, ctx = _mgr_and_ctx()
    _ingest_v1(mgr)
    with pytest.raises(DocumentMutationError):
        mgr.purge(ctx, corpus_id="eng", document_id="doc1")


def test_tighten_acl_patches_payloads_without_reembedding() -> None:
    mgr, mstore, vstore, _, ctx = _mgr_and_ctx()
    _ingest_v1(mgr)
    tight = ResourceAcl(
        tenant_id="t1", security_level="public", acl_scope="restricted",
        allowed_user_ids=["u2"], allowed_group_ids=[], denied_user_ids=[], denied_group_ids=[],
    )
    mgr.tighten_acl(ctx, corpus_id="eng", document_id="doc1", acl=tight)

    ids = vstore.list_point_ids_by_document("eng", "t1", "eng", "doc1", "v1")
    retrieved = vstore._client.retrieve(collection_name="eng", ids=ids, with_payload=True)
    assert all(
        p.payload["acl_scope"] == "restricted" and p.payload["allowed_user_ids"] == ["u2"]
        for p in retrieved
    )
    doc = mstore.get_document("t1", "eng", "doc1", "v1")
    assert doc.acl_scope == "restricted"
    assert doc.allowed_user_ids == ["u2"]


def test_cross_tenant_mutation_is_refused() -> None:
    mgr, _, _, _, ctx = _mgr_and_ctx()
    _ingest_v1(mgr)
    other = make_security_context(tenant_id="t2")
    with pytest.raises(DocumentMutationError):
        mgr.delete(other, corpus_id="eng", document_id="doc1")
    with pytest.raises(DocumentMutationError):
        mgr.tighten_acl(
            other, corpus_id="eng", document_id="doc1",
            acl=ResourceAcl(tenant_id="t1", security_level="public", acl_scope="tenant"),
        )
    with pytest.raises(DocumentMutationError):
        mgr.update(
            other, corpus_id="eng", document_id="doc1",
            content="x", job_id="jx", document_version="v2",
        )


def test_non_discoverable_corpus_mutation_is_refused() -> None:
    mgr, _, _, _, _ = _mgr_and_ctx()
    _ingest_v1(mgr)
    hidden = make_security_context(allowed_corpus_ids=["other-corpus"])
    with pytest.raises(DocumentMutationError):
        mgr.delete(hidden, corpus_id="eng", document_id="doc1")
