"""Shared test fixtures: deterministic fake encoders and sample data.

These keep E-007 tests hermetic — no embedding models are downloaded and no
network is touched. Real encoders are injected at runtime via the storage and
retrieval constructors, so the same code paths run with fake or real vectors.
"""

import hashlib
from typing import Any

from qdrant_client.models import PointStruct, SparseVector

from agentic_rag_enterprise.domain.security import SecurityContext
from agentic_rag_enterprise.ingestion.chunker import ParentChunk
from agentic_rag_enterprise.storage.vector_store import DenseEncoder, SparseEncoder

DENSE_DIM = 4


class FakeDenseEncoder(DenseEncoder):
    """Deterministic dense encoder (hash -> fixed-dim vector)."""

    def __call__(self, text: str) -> list[float]:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        return [float(b) / 255.0 for b in digest[:DENSE_DIM]]


class FakeSparseEncoder(SparseEncoder):
    """Deterministic sparse encoder (word hashes -> indices/values)."""

    def __call__(self, text: str) -> SparseVector:
        indices: list[int] = []
        values: list[float] = []
        words = sorted({w for w in text.split() if w})[:8]
        for word in words:
            idx = int(hashlib.sha256(word.encode("utf-8")).hexdigest(), 16) % 1000
            indices.append(idx)
            values.append(1.0)
        return SparseVector(indices=indices, values=values)


def make_security_context(
    *,
    tenant_id: str = "t1",
    user_id: str = "u1",
    groups: list[str] | None = None,
    allowed_security_levels: list[str] | None = None,
    allowed_corpus_ids: list[str] | None = None,
    is_admin: bool = False,
) -> SecurityContext:
    return SecurityContext(
        request_id="r",
        session_id="s",
        tenant_id=tenant_id,
        user_id=user_id,
        groups=groups or [],
        allowed_security_levels=(
            allowed_security_levels
            if allowed_security_levels is not None
            else ["public", "internal"]
        ),
        allowed_corpus_ids=allowed_corpus_ids,
        policy_version="1.0",
        is_admin=is_admin,
    )


def acl_payload(
    *,
    tenant_id: str = "t1",
    security_level: str = "public",
    acl_scope: str = "tenant",
    allowed_user_ids: list[str] | None = None,
    allowed_group_ids: list[str] | None = None,
    denied_user_ids: list[str] | None = None,
    denied_group_ids: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "tenant_id": tenant_id,
        "security_level": security_level,
        "acl_scope": acl_scope,
        "allowed_user_ids": allowed_user_ids or [],
        "allowed_group_ids": allowed_group_ids or [],
        "denied_user_ids": denied_user_ids or [],
        "denied_group_ids": denied_group_ids or [],
    }


def make_child_point(
    point_id: int,
    text: str,
    *,
    tenant_id: str,
    corpus_id: str,
    document_id: str,
    document_version: str,
    parent_id: str,
    acl: dict[str, Any],
    status: str = "active",
    deprecated: bool = False,
) -> PointStruct:
    payload = {
        "tenant_id": tenant_id,
        "corpus_id": corpus_id,
        "document_id": document_id,
        "document_version": document_version,
        "parent_id": parent_id,
        "chunk_id": f"{parent_id}:0",
        "text": text,
        "status": status,
        "deprecated": deprecated,
        "section_path": [],
    }
    payload.update(acl)
    dense = FakeDenseEncoder()(text)
    sparse = FakeSparseEncoder()(text)
    return PointStruct(id=point_id, vector={"": dense, "sparse": sparse}, payload=payload)


def make_parent_chunk(
    parent_id: str,
    text: str,
    *,
    tenant_id: str,
    corpus_id: str,
    document_id: str,
    document_version: str,
    acl: dict[str, Any],
    status: str = "active",
    deprecated: bool = False,
) -> ParentChunk:
    metadata = {
        "status": status,
        "deprecated": deprecated,
        "document_version": document_version,
        "security_level": acl["security_level"],
        "acl_scope": acl["acl_scope"],
        "allowed_user_ids": acl.get("allowed_user_ids", []),
        "allowed_group_ids": acl.get("allowed_group_ids", []),
        "denied_user_ids": acl.get("denied_user_ids", []),
        "denied_group_ids": acl.get("denied_group_ids", []),
    }
    return ParentChunk(
        parent_id=parent_id,
        document_id=document_id,
        document_version=document_version,
        tenant_id=tenant_id,
        corpus_id=corpus_id,
        text=text,
        section_path=[],
        metadata=metadata,
    )


SAMPLE_MARKDOWN = """# System Overview

The agentic RAG system routes queries across corpora.

## Architecture

The runtime uses a planner and a sufficiency judge.

### Planner

The planner selects corpora based on the question.

## Security

Access is enforced at retrieval time with a policy decision point.
"""


def active_metadata_store(
    tenant_id: str,
    corpus_id: str,
    document_id: str,
    version: str,
    *,
    content_hash: str = "seed",
) -> "MetadataStore":  # noqa: F821 - imported lazily to avoid test-only deps
    """Build a hermetic MetadataStore with one ACTIVE document version.

    Used by E-007 retrieval tests now that the control-plane active-version gate
    is mandatory (E-008.2 P1-7): retrieval drops child hits whose version is not
    the Metadata DB's active version, so the gate must know the seeded version is
    active. The seeded version matches the points/parents the tests ingest.
    """
    import os
    import tempfile
    from datetime import datetime, timezone

    from agentic_rag_enterprise.domain.document import SourceDocument
    from agentic_rag_enterprise.domain.ingestion import DocumentStatus
    from agentic_rag_enterprise.storage.metadata_store import MetadataStore

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    store = MetadataStore(path)
    now = datetime(2024, 1, 1, tzinfo=timezone.utc)
    store._conn.execute(  # noqa: SLF001 - test fixture reaches into raw conn
        """
        INSERT INTO corpus_registry (
            corpus_id, tenant_id, name, description, created_at, updated_at
        ) VALUES (?, ?, 'corpus', '', ?, ?)
        """,
        (corpus_id, tenant_id, now.isoformat(), now.isoformat()),
    )
    doc = SourceDocument(
        document_id=document_id,
        tenant_id=tenant_id,
        corpus_id=corpus_id,
        source_uri=f"inline://{document_id}",
        source_connector="file",
        title=document_id,
        source_filename=f"{document_id}.md",
        mime_type="text/markdown",
        version=version,
        content_hash=content_hash,
        status=DocumentStatus.ACTIVE,
        authority_level=50,
        deprecated=False,
        acl_policy_id="default",
        security_level="public",
        acl_scope="tenant",
        allowed_user_ids=["u1"],
        allowed_group_ids=["g1"],
        denied_user_ids=[],
        denied_group_ids=[],
        parser_name="markdown",
        parser_version="1.0",
        chunking_version="1.0",
        embedding_model="fake",
        embedding_version="1.0",
        discovered_at=now,
        indexed_at=now,
        last_synced_at=now,
    )
    store.upsert_document(doc)
    return store
