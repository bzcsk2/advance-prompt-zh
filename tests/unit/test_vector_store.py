"""Unit tests for VectorStore payload/list helpers (E-010).

Verifies ACL tightening patches payloads WITHOUT re-embedding, and that
document-scoped point listing never addresses another document's data plane.
"""

from qdrant_client import QdrantClient

from agentic_rag_enterprise.ingestion.chunker import ChildChunk
from agentic_rag_enterprise.security.policy import ResourceAcl
from agentic_rag_enterprise.storage.vector_store import VectorStore, child_chunk_to_point

from tests.fixtures import (
    DENSE_DIM,
    FakeDenseEncoder,
    FakeSparseEncoder,
    acl_payload,
)


class _CountingDenseEncoder(FakeDenseEncoder):
    """Fake encoder that counts invocations so we can prove no re-embed."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, text: str) -> list[float]:
        self.calls += 1
        return super().__call__(text)


def _make_point(child_id: str, document_id: str, version: str) -> object:
    acl = ResourceAcl(**acl_payload(tenant_id="t1", acl_scope="tenant", security_level="public"))
    chunk = ChildChunk(
        child_id=child_id,
        parent_id="p",
        document_id=document_id,
        document_version=version,
        tenant_id="t1",
        corpus_id="eng",
        text="sample child text",
    )
    return child_chunk_to_point(
        chunk,
        acl,
        status="active",
        deprecated=False,
        dense_encoder=FakeDenseEncoder(),
        sparse_encoder=FakeSparseEncoder(),
    )


def _client_and_store():
    client = QdrantClient(location=":memory:")
    store = VectorStore(client)
    store.create_collection("eng", dense_size=DENSE_DIM)
    return client, store


def test_update_payload_patches_without_reembedding() -> None:
    client, store = _client_and_store()
    encoder = _CountingDenseEncoder()
    point = child_chunk_to_point(
        ChildChunk(
            child_id="c1", parent_id="p", document_id="d1",
            document_version="v1", tenant_id="t1", corpus_id="eng", text="x",
        ),
        ResourceAcl(**acl_payload(tenant_id="t1", acl_scope="tenant", security_level="public")),
        status="active", deprecated=False, dense_encoder=encoder, sparse_encoder=FakeSparseEncoder(),
    )
    store.upsert("eng", [point])
    calls_after_upsert = encoder.calls

    # ACL tightening: patch only ACL fields.
    store.update_payload(
        "eng", [str(point.id)],
        {"acl_scope": "restricted", "allowed_user_ids": ["u2"]},
    )

    assert encoder.calls == calls_after_upsert, "update_payload must not re-embed"
    retrieved = client.retrieve(
        collection_name="eng", ids=[point.id], with_payload=True, with_vectors=True
    )
    payload = retrieved[0].payload
    assert payload["acl_scope"] == "restricted"
    assert payload["allowed_user_ids"] == ["u2"]
    # Vectors untouched (set_payload never re-embeds).
    assert retrieved[0].vector is not None


def test_list_point_ids_by_document_is_scoped() -> None:
    client, store = _client_and_store()
    p1 = _make_point("c1", "d1", "v1")
    p2 = _make_point("c2", "d1", "v1")
    p3 = _make_point("c3", "d2", "v1")
    store.upsert("eng", [p1, p2, p3])
    ids = store.list_point_ids_by_document("eng", "t1", "eng", "d1", "v1")
    assert len(ids) == 2
    d2_id = str(p3.id)
    assert d2_id not in ids
    assert str(p1.id) in ids and str(p2.id) in ids
