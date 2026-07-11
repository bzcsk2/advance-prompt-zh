"""Characterization tests for enterprise domain models, lifecycle, and migrations."""

from datetime import datetime

import pytest

from agentic_rag_enterprise.domain.chunk import ChunkRecord
from agentic_rag_enterprise.domain.common import CorpusId, DocumentId, TenantId
from agentic_rag_enterprise.domain.corpus import CorpusConfig
from agentic_rag_enterprise.domain.document import SourceDocument
from agentic_rag_enterprise.domain.evidence import Evidence
from agentic_rag_enterprise.domain.ingestion import (
    DOCUMENT_LIFECYCLE,
    DocumentStatus,
    IngestionManifest,
    JobStatus,
    valid_transition,
)
from agentic_rag_enterprise.domain.security import SecurityContext


# =============================================================================
# Common type aliases
# =============================================================================


def test_newtype_identity() -> None:
    tid: TenantId = TenantId("tenant-1")
    cid: CorpusId = CorpusId("corpus-1")
    did: DocumentId = DocumentId("doc-1")
    assert str(tid) == "tenant-1"
    assert str(cid) == "corpus-1"
    assert str(did) == "doc-1"


# =============================================================================
# CorpusConfig
# =============================================================================


def test_corpus_config_minimal() -> None:
    now = datetime(2026, 1, 1)
    cfg = CorpusConfig(
        corpus_id="product_docs",
        tenant_id="local",
        name="Product Docs",
        description="Product documentation",
        domain="product",
        owner="product-team",
        source_type="documents",
        capability_ids=["vector_search"],
        security_policy_id="local_internal",
        created_at=now,
        updated_at=now,
    )
    assert cfg.corpus_id == "product_docs"
    assert cfg.authority_level == 50
    assert cfg.enabled is True


def test_corpus_config_authority_bounds() -> None:
    now = datetime(2026, 1, 1)
    with pytest.raises(ValueError):
        CorpusConfig(
            corpus_id="x",
            tenant_id="t",
            name="x",
            description="x",
            domain="x",
            owner="x",
            source_type="documents",
            capability_ids=[],
            security_policy_id="p",
            authority_level=150,
            created_at=now,
            updated_at=now,
        )


# =============================================================================
# SourceDocument
# =============================================================================


def test_source_document_minimal() -> None:
    now = datetime(2026, 1, 1)
    doc = SourceDocument(
        document_id="doc-1",
        tenant_id="t1",
        corpus_id="c1",
        source_uri="/path/doc.md",
        source_connector="file",
        title="Doc",
        source_filename="doc.md",
        mime_type="text/markdown",
        version="v1",
        content_hash="abc123",
        status="discovered",
        acl_policy_id="default",
        security_level="internal",
        parser_name="markdown",
        parser_version="1.0",
        chunking_version="1.0",
        embedding_model="mock",
        embedding_version="1.0",
        discovered_at=now,
        last_synced_at=now,
    )
    assert doc.document_id == "doc-1"
    assert doc.status == "discovered"
    assert doc.acl_scope == "restricted"


def test_source_document_all_statuses() -> None:
    now = datetime(2026, 1, 1)
    base = dict(
        tenant_id="t",
        corpus_id="c",
        source_uri="u",
        source_connector="file",
        title="t",
        source_filename="f",
        mime_type="t",
        version="v",
        content_hash="h",
        acl_policy_id="d",
        security_level="internal",
        parser_name="p",
        parser_version="1",
        chunking_version="1",
        embedding_model="m",
        embedding_version="1",
        discovered_at=now,
        last_synced_at=now,
    )
    for status in ("discovered", "processing", "active", "failed", "deprecated", "deleted"):
        doc = SourceDocument(document_id="d", status=status, **base)  # type: ignore[arg-type]
        assert doc.status == status


# =============================================================================
# ChunkRecord
# =============================================================================


def test_chunk_record_child() -> None:
    chunk = ChunkRecord(
        chunk_id="ch-1",
        tenant_id="t1",
        corpus_id="c1",
        document_id="doc-1",
        document_version="v1",
        chunk_type="child",
        content="hello",
        content_hash="abc",
        acl_policy_id="default",
        security_level="internal",
    )
    assert chunk.chunk_type == "child"
    assert chunk.page_number is None
    assert chunk.section_path == []


def test_chunk_record_parent() -> None:
    chunk = ChunkRecord(
        chunk_id="parent-1",
        tenant_id="t1",
        corpus_id="c1",
        document_id="doc-1",
        document_version="v1",
        chunk_type="parent",
        content="parent content",
        content_hash="def",
        page_number=3,
        section_path=["Introduction", "Overview"],
        start_offset=0,
        end_offset=100,
        acl_policy_id="default",
        security_level="internal",
    )
    assert chunk.chunk_type == "parent"
    assert chunk.page_number == 3
    assert chunk.section_path == ["Introduction", "Overview"]


# =============================================================================
# SecurityContext
# =============================================================================


def test_security_context_defaults() -> None:
    ctx = SecurityContext(
        request_id="req-1",
        session_id="ses-1",
        tenant_id="t1",
        user_id="u1",
        policy_version="1.0",
    )
    assert ctx.tenant_id == "t1"
    assert ctx.allowed_security_levels == ["public", "internal"]
    assert ctx.is_admin is False


def test_security_context_with_roles() -> None:
    ctx = SecurityContext(
        request_id="r1",
        session_id="s1",
        tenant_id="t1",
        user_id="u1",
        roles=["admin", "reader"],
        groups=["eng"],
        allowed_corpus_ids=["docs", "wiki"],
        policy_version="2.0",
        is_admin=True,
    )
    assert "admin" in ctx.roles
    assert ctx.allowed_corpus_ids == ["docs", "wiki"]
    assert ctx.is_admin is True


# =============================================================================
# Evidence
# =============================================================================


def test_evidence_minimal() -> None:
    now = datetime(2026, 1, 1)
    ev = Evidence(
        evidence_id="ev-1",
        tenant_id="t1",
        corpus_id="c1",
        document_id="doc-1",
        document_version="v1",
        source_uri="/path/doc.md",
        source_filename="doc.md",
        text="evidence text",
        text_hash="abc123",
        retrieval_query="test query",
        authority_level=50,
        retrieved_at=now,
        acl_policy_id="default",
        policy_version="1.0",
        retrieval_iteration=1,
    )
    assert ev.evidence_id == "ev-1"
    assert ev.retrieval_iteration == 1
    assert ev.plan_step_id is None


# =============================================================================
# Ingestion lifecycle
# =============================================================================


def test_document_status_enum_values() -> None:
    assert DocumentStatus.DISCOVERED.value == "discovered"
    assert DocumentStatus.ACTIVE.value == "active"
    assert DocumentStatus.DELETED.value == "deleted"


def test_job_status_enum_values() -> None:
    assert JobStatus.QUEUED.value == "queued"
    assert JobStatus.SUCCEEDED.value == "succeeded"


def test_valid_transition_discovered_to_processing() -> None:
    assert valid_transition(DocumentStatus.DISCOVERED, DocumentStatus.PROCESSING) is True


def test_valid_transition_processing_to_active() -> None:
    assert valid_transition(DocumentStatus.PROCESSING, DocumentStatus.ACTIVE) is True


def test_valid_transition_active_to_deleted() -> None:
    assert valid_transition(DocumentStatus.ACTIVE, DocumentStatus.DELETED) is True


def test_valid_transition_active_to_deprecated() -> None:
    assert valid_transition(DocumentStatus.ACTIVE, DocumentStatus.DEPRECATED) is True


def test_valid_transition_deprecated_to_deleted() -> None:
    assert valid_transition(DocumentStatus.DEPRECATED, DocumentStatus.DELETED) is True


def test_valid_transition_deleted_has_no_outgoing() -> None:
    assert valid_transition(DocumentStatus.DELETED, DocumentStatus.ACTIVE) is False
    assert valid_transition(DocumentStatus.DELETED, DocumentStatus.PROCESSING) is False


def test_invalid_transition_discovered_to_active() -> None:
    assert valid_transition(DocumentStatus.DISCOVERED, DocumentStatus.ACTIVE) is False


def test_invalid_transition_active_to_discovered() -> None:
    assert valid_transition(DocumentStatus.ACTIVE, DocumentStatus.DISCOVERED) is False


def test_document_lifecycle_has_all_statuses() -> None:
    for status in DocumentStatus:
        assert status in DOCUMENT_LIFECYCLE


# =============================================================================
# IngestionManifest
# =============================================================================


def test_ingestion_manifest_minimal() -> None:
    now = datetime(2026, 1, 1)
    manifest = IngestionManifest(
        job_id="job-1",
        document_id="doc-1",
        document_version="v1",
        corpus_id="c1",
        status="running",
        started_at=now,
        raw_hash="abc",
        parser_version="1.0",
        chunking_version="1.0",
        embedding_version="1.0",
    )
    assert manifest.job_id == "job-1"
    assert manifest.child_count == 0
    assert manifest.error_code is None


# =============================================================================
# Migration scaffolding
# =============================================================================


def test_initial_migration_exists() -> None:
    import os

    path = os.path.join(os.path.dirname(__file__), "..", "migrations", "001_initial_schema.sql")
    assert os.path.isfile(path), "Initial migration file must exist"
    with open(path) as f:
        content = f.read()
    assert "corpus_registry" in content
    assert "documents" in content
    assert "ingestion_jobs" in content
