"""Index migration + rollback (E-022, build plan §10.8).

Builds a **new** Qdrant collection alongside the current one (never in-place),
re-embedding the existing child-chunk content + ACL into it, then switches
retrieval to it via an atomic registry/MetadataStore pointer flip. The previous
collection is **retained** for rollback (it is never cleared-and-rebuilt).

The retrieval pointer is ``CorpusConfig.vector_collection`` (the hybrid retriever
already queries ``corpus.vector_collection or corpus_id``), so flipping it
switches live retrieval with no change to the answer pipeline.

Note: the canonical ingestion pipeline (``IngestionJob``) continues to write to
the ``corpus_id`` collection. A migrated ``v2`` index is a parallel evaluation
index per §10.8 ("build v2 → offline eval → shadow retrieval → switch pointer →
observe → retain v1 → purge later"); long-term operation re-runs
:func:`build_index_v2` after content changes.
"""

from __future__ import annotations

import json
import uuid
from typing import Optional

from qdrant_client.models import PointStruct

from agentic_rag_enterprise.corpus.registry import CorpusRegistry
from agentic_rag_enterprise.storage.metadata_store import (
    IndexSwitchConflict,
    MetadataStore,
)
from agentic_rag_enterprise.storage.vector_store import (
    DEFAULT_SPARSE_NAME,
    DenseEncoder,
    SparseEncoder,
    VectorStore,
    child_point_id,
)


def _parse_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return []
    return list(value)


def _chunk_row_to_point(
    row: dict, *, dense_encoder: DenseEncoder, sparse_encoder: SparseEncoder
) -> PointStruct:
    """Reconstruct a Qdrant point from a control-plane child-chunk record.

    Mirrors the payload written by :func:`child_chunk_to_point` so a migrated
    index is byte-for-byte interchangeable with the source for retrieval.
    """
    dense = dense_encoder(row["content"])
    sparse = sparse_encoder(row["content"])
    payload = {
        "tenant_id": row["tenant_id"],
        "corpus_id": row["corpus_id"],
        "document_id": row["document_id"],
        "document_version": row["document_version"],
        "parent_id": row.get("parent_id"),
        "chunk_id": row["chunk_id"],
        "text": row["content"],
        "section_path": _parse_list(row.get("section_path")),
        "status": "active",
        "deprecated": False,
        "security_level": row.get("security_level", "internal"),
        "acl_scope": row.get("acl_scope", "restricted"),
        "allowed_user_ids": _parse_list(row.get("allowed_user_ids")),
        "allowed_group_ids": _parse_list(row.get("allowed_group_ids")),
        "denied_user_ids": _parse_list(row.get("denied_user_ids")),
        "denied_group_ids": _parse_list(row.get("denied_group_ids")),
    }
    return PointStruct(
        id=child_point_id(row["chunk_id"]),
        vector={"": dense, "sparse": sparse},
        payload=payload,
    )


def new_collection_name(corpus_id: str, *, embedding_version: str, chunking_version: str) -> str:
    """Deterministic v2 collection name (build plan §10.8)."""
    return f"{corpus_id}_v{embedding_version}_{chunking_version}"


def build_index_v2(
    corpus_id: str,
    *,
    embedding_version: str,
    chunking_version: str,
    dense_size: int,
    metadata_store: MetadataStore,
    vector_store: VectorStore,
    corpus_registry: CorpusRegistry,
    dense_encoder: DenseEncoder,
    sparse_encoder: SparseEncoder,
    build_id: Optional[str] = None,
) -> str:
    """Build a parallel ``v2`` collection from existing child-chunk content.

    Creates ``corpus_id_v{emb}_{chunk}`` (never touching the live collection),
    re-embeds every active child chunk into it, and records the build with its
    ``previous_collection`` so it can later be rolled back. Returns the new
    collection name.
    """
    previous_collection = corpus_registry.resolve_collection_name(corpus_id)
    collection = new_collection_name(
        corpus_id, embedding_version=embedding_version, chunking_version=chunking_version
    )
    vector_store.create_collection(collection, dense_size, sparse_name=DEFAULT_SPARSE_NAME)

    # Only the active, non-deprecated versions are migrated — this is what stops
    # the v2 index from resurrecting deprecated or logically-deleted evidence
    # (E-022 contract P1-1).
    rows = metadata_store.iter_active_child_chunks(corpus_id)
    points = [
        _chunk_row_to_point(row, dense_encoder=dense_encoder, sparse_encoder=sparse_encoder)
        for row in rows
    ]
    vector_store.upsert(collection, points)

    bid = build_id or uuid.uuid4().hex
    metadata_store.begin_index_build(
        bid,
        corpus_id,
        collection,
        embedding_version,
        chunking_version,
        previous_collection,
    )
    metadata_store.complete_index_build(bid)
    return collection


def _switch_pointer(
    corpus_id: str,
    target_collection: str,
    *,
    metadata_store: MetadataStore,
    corpus_registry: CorpusRegistry,
    vector_store: VectorStore,
    owner: str,
) -> None:
    """Leased, atomic pointer flip shared by switch + rollback (P1-3).

    1. Acquire the per-corpus index-switch lease (rejected if another switch
       owns it -> :class:`IndexSwitchConflict`).
    2. Flip the persisted pointer + mark the live build ``switched`` inside one
       ``BEGIN IMMEDIATE`` (``MetadataStore.set_active_collection_atomic``).
    3. Mirror the flip onto the live in-memory registry. If that fails, the
       persisted pointer is *compensated* back to its previous value so the two
       never diverge, then the error is re-raised.
    The lease is always released (success or failure).
    """
    if not vector_store.collection_exists(target_collection):
        raise ValueError(f"target collection {target_collection!r} does not exist")
    # Exact prior pointer (may be None); used only to compensate on failure.
    previous = metadata_store.get_active_collection(corpus_id)
    if not metadata_store.acquire_index_switch_lease(corpus_id, owner):
        raise IndexSwitchConflict(f"index switch for corpus {corpus_id!r} is already in progress")
    try:
        metadata_store.set_active_collection_atomic(corpus_id, target_collection, owner)
        try:
            corpus_registry.set_active_collection(corpus_id, target_collection)
        except Exception:
            # Compensate: revert the persisted pointer so it stays consistent
            # with the live registry (which still points at `previous`).
            metadata_store.set_active_collection_atomic(corpus_id, previous, owner)
            raise
    finally:
        metadata_store.release_index_switch_lease(corpus_id, owner)


def switch_index(
    corpus_id: str,
    *,
    target_collection: str,
    metadata_store: MetadataStore,
    corpus_registry: CorpusRegistry,
    vector_store: VectorStore,
    owner: Optional[str] = None,
    dry_run: bool = False,
) -> None:
    """Atomically flip the active-collection pointer to ``target_collection``.

    The switch is lease-guarded and flips the persisted
    ``corpus_registry.vector_collection`` and the live :class:`CorpusConfig` the
    retriever reads inside one transaction, with compensation if the live
    registry update fails (E-022 contract P1-3). The previous collection is
    retained (never deleted) so a rollback is always possible.
    """
    if dry_run:
        if not vector_store.collection_exists(target_collection):
            raise ValueError(f"target collection {target_collection!r} does not exist")
        return
    owner = owner or f"index-switch-{uuid.uuid4().hex[:8]}"
    _switch_pointer(
        corpus_id,
        target_collection,
        metadata_store=metadata_store,
        corpus_registry=corpus_registry,
        vector_store=vector_store,
        owner=owner,
    )


def rollback_index(
    corpus_id: str,
    *,
    metadata_store: MetadataStore,
    corpus_registry: CorpusRegistry,
    vector_store: VectorStore,
    owner: Optional[str] = None,
) -> str:
    """Flip the active pointer back to the collection retained at last build.

    Returns the collection name switched back to. Raises ``ValueError`` if there
    is no retained previous collection (e.g. the corpus was never migrated). Uses
    the same leased, atomic, compensated flip as :func:`switch_index` (P1-3).
    """
    # The most recent build for this corpus records the collection that was
    # active when the build started — that is the rollback target.
    row = metadata_store._conn.execute(  # type: ignore[attr-defined]
        "SELECT previous_collection FROM index_builds "
        "WHERE corpus_id=? AND previous_collection IS NOT NULL "
        "ORDER BY started_at DESC LIMIT 1",
        (corpus_id,),
    ).fetchone()
    if row is None or not row["previous_collection"]:
        raise ValueError(f"no retained previous collection to roll back for {corpus_id!r}")
    previous = row["previous_collection"]
    owner = owner or f"index-rollback-{uuid.uuid4().hex[:8]}"
    _switch_pointer(
        corpus_id,
        previous,
        metadata_store=metadata_store,
        corpus_registry=corpus_registry,
        vector_store=vector_store,
        owner=owner,
    )
    return previous
