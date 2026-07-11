"""Qdrant vector store with dense + sparse hybrid retrieval.

The store is deliberately **authorization-agnostic**: it never decides which
ACL conditions apply to a caller. Every search requires a pre-built
``qdrant_client.models.Filter`` (produced by
:func:`agentic_rag_enterprise.security.filter.build_access_filter`); a ``None``
filter is rejected so retrieval can never become filter-less.

Encoders are injected so tests stay hermetic (deterministic fake encoders,
no model downloads) and so real embeddings can be swapped in without touching
this module.
"""

from typing import Protocol, runtime_checkable
from uuid import NAMESPACE_URL, uuid5

from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    Filter,
    Fusion,
    FusionQuery,
    PointStruct,
    Prefetch,
    SparseVector,
    SparseVectorParams,
    VectorParams,
)

from agentic_rag_enterprise.ingestion.chunker import ChildChunk
from agentic_rag_enterprise.security.policy import ResourceAcl

# Stable namespace for deriving Qdrant point ids (UUIDs) from business chunk ids.
_CHUNK_NAMESPACE = uuid5(NAMESPACE_URL, "agentic-rag-enterprise:child-chunk")

DEFAULT_SPARSE_NAME = "sparse"


@runtime_checkable
class DenseEncoder(Protocol):
    """Maps text to a dense vector."""

    def __call__(self, text: str) -> list[float]: ...


@runtime_checkable
class SparseEncoder(Protocol):
    """Maps text to a sparse vector."""

    def __call__(self, text: str) -> SparseVector: ...


class VectorStore:
    """Thin wrapper over a Qdrant client for hybrid child retrieval."""

    def __init__(self, client: QdrantClient) -> None:
        self._client = client

    def create_collection(
        self,
        name: str,
        dense_size: int,
        distance: Distance = Distance.COSINE,
        sparse_name: str = DEFAULT_SPARSE_NAME,
    ) -> None:
        if self._client.collection_exists(name):
            return
        self._client.create_collection(
            collection_name=name,
            vectors_config=VectorParams(size=dense_size, distance=distance),
            sparse_vectors_config={sparse_name: SparseVectorParams()},
        )

    def upsert(self, name: str, points: list[PointStruct]) -> None:
        if points:
            self._client.upsert(collection_name=name, points=points)

    def search(
        self,
        name: str,
        query_text: str,
        *,
        filter: Filter | None,
        top_k: int,
        dense_encoder: DenseEncoder,
        sparse_encoder: SparseEncoder,
        sparse_name: str = DEFAULT_SPARSE_NAME,
    ):
        """Hybrid (RRF-fused) search gated by a mandatory authorization filter.

        Returns a list of ``qdrant_client.models.ScoredPoint``. Raises
        ``ValueError`` if ``filter`` is ``None`` so retrieval can never run
        without an ACL filter.
        """
        if filter is None:
            raise ValueError(
                "refusing filter-less retrieval: build_access_filter result is required"
            )

        dense = dense_encoder(query_text)
        sparse = sparse_encoder(query_text)
        # Apply the ACL filter on each prefetch (the fusion root query does not
        # consistently honor query_filter in local/Qdrant, so we filter before
        # fusion). The root query_filter is retained as defense in depth.
        prefetch = [
            Prefetch(query=dense, using="", filter=filter),
            Prefetch(query=sparse, using=sparse_name, filter=filter),
        ]

        response = self._client.query_points(
            collection_name=name,
            query=FusionQuery(fusion=Fusion.RRF),
            prefetch=prefetch,
            query_filter=filter,
            limit=top_k,
            with_payload=True,
        )
        return response.points

    def close(self) -> None:
        self._client.close()


def child_chunk_to_point(
    child: ChildChunk,
    acl: ResourceAcl,
    *,
    status: str,
    deprecated: bool,
    dense_encoder: DenseEncoder,
    sparse_encoder: SparseEncoder,
) -> PointStruct:
    """Production mapping from a chunked :class:`ChildChunk` to a Qdrant point.

    The business id is ``child.child_id`` (content-addressed). The Qdrant point
    id is a *stable UUID* derived from it, so it is always a valid Qdrant id
    (``u64``/``Uuid``) regardless of the business id's representation. The payload
    carries full provenance + ACL so the retrieval path can re-establish
    identity and authorization at read time.
    """
    dense = dense_encoder(child.text)
    sparse = sparse_encoder(child.text)
    payload = {
        "tenant_id": child.tenant_id,
        "corpus_id": child.corpus_id,
        "document_id": child.document_id,
        "document_version": child.document_version,
        "parent_id": child.parent_id,
        "chunk_id": child.child_id,
        "text": child.text,
        "section_path": child.section_path,
        "status": status,
        "deprecated": deprecated,
        "security_level": acl.security_level,
        "acl_scope": acl.acl_scope,
        "allowed_user_ids": acl.allowed_user_ids,
        "allowed_group_ids": acl.allowed_group_ids,
        "denied_user_ids": acl.denied_user_ids,
        "denied_group_ids": acl.denied_group_ids,
    }
    return PointStruct(
        id=str(uuid5(_CHUNK_NAMESPACE, child.child_id)),
        vector={"": dense, "sparse": sparse},
        payload=payload,
    )


# Re-export for callers that build sparse vectors without importing qdrant directly.
__all__ = [
    "VectorStore",
    "DenseEncoder",
    "SparseEncoder",
    "SparseVector",
    "child_chunk_to_point",
]
