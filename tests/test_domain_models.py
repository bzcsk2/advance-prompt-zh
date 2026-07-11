"""Characterization tests for enterprise domain models, lifecycle, and migrations."""

import sqlite3
from datetime import datetime

import pytest

from agentic_rag_enterprise.domain.chunk import ChunkRecord
from agentic_rag_enterprise.domain.common import CorpusId, DocumentId, TenantId
from agentic_rag_enterprise.domain.corpus import CorpusConfig
from agentic_rag_enterprise.domain.document import SourceDocument
from agentic_rag_enterprise.domain.evidence import Evidence
from agentic_rag_enterprise.domain.ingestion import (
    DOCUMENT_LIFECYCLE,
    JOB_LIFECYCLE,
    DocumentStatus,
    IngestionManifest,
    JobStatus,
    valid_job_transition,
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


def _base_doc_kwargs(now: datetime) -> dict:
    return dict(
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


def test_source_document_minimal() -> None:
    now = datetime(2026, 1, 1)
    doc = SourceDocument(
        document_id="doc-1",
        status=DocumentStatus.DISCOVERED,
        **_base_doc_kwargs(now),
    )
    assert doc.document_id == "doc-1"
    assert doc.status == DocumentStatus.DISCOVERED
    assert doc.acl_scope == "restricted"


def test_source_document_all_statuses() -> None:
    now = datetime(2026, 1, 1)
    base = _base_doc_kwargs(now)
    for status in DocumentStatus:
        kwargs = dict(base, discovered_at=now, last_synced_at=now)
        if status == DocumentStatus.ACTIVE:
            kwargs["indexed_at"] = now
        if status == DocumentStatus.DELETED:
            kwargs["deleted_at"] = now
        doc = SourceDocument(document_id="d", status=status, **kwargs)
        assert doc.status == status


def test_active_document_requires_indexed_at() -> None:
    now = datetime(2026, 1, 1)
    with pytest.raises(ValueError, match="indexed_at"):
        SourceDocument(
            document_id="d",
            status=DocumentStatus.ACTIVE,
            **_base_doc_kwargs(now),
        )


def test_deleted_document_requires_deleted_at() -> None:
    now = datetime(2026, 1, 1)
    with pytest.raises(ValueError, match="deleted_at"):
        SourceDocument(
            document_id="d",
            status=DocumentStatus.DELETED,
            **_base_doc_kwargs(now),
        )


def test_document_authority_bounds() -> None:
    now = datetime(2026, 1, 1)
    with pytest.raises(ValueError, match="authority_level"):
        SourceDocument(
            document_id="d",
            status=DocumentStatus.DISCOVERED,
            authority_level=200,
            **_base_doc_kwargs(now),
        )


def test_document_effective_to_before_from() -> None:
    now = datetime(2026, 1, 1)
    later = datetime(2026, 6, 1)
    with pytest.raises(ValueError, match="effective_to"):
        SourceDocument(
            document_id="d",
            status=DocumentStatus.DISCOVERED,
            effective_from=later,
            effective_to=now,
            **_base_doc_kwargs(now),
        )


def test_document_acl_empty_allowed() -> None:
    now = datetime(2026, 1, 1)
    doc = SourceDocument(
        document_id="d",
        status=DocumentStatus.DISCOVERED,
        acl_scope="restricted",
        allowed_user_ids=[],
        allowed_group_ids=[],
        denied_user_ids=[],
        denied_group_ids=[],
        **_base_doc_kwargs(now),
    )
    assert doc.allowed_user_ids == []


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


def test_chunk_authority_bounds() -> None:
    with pytest.raises(ValueError, match="authority_level"):
        ChunkRecord(
            chunk_id="c",
            tenant_id="t",
            corpus_id="c",
            document_id="d",
            document_version="v",
            chunk_type="child",
            content="x",
            content_hash="h",
            authority_level=150,
            acl_policy_id="d",
            security_level="internal",
        )


def test_chunk_start_offset_non_negative() -> None:
    with pytest.raises(ValueError, match="start_offset"):
        ChunkRecord(
            chunk_id="c",
            tenant_id="t",
            corpus_id="c",
            document_id="d",
            document_version="v",
            chunk_type="child",
            content="x",
            content_hash="h",
            start_offset=-1,
            acl_policy_id="d",
            security_level="internal",
        )


def test_chunk_end_offset_less_than_start() -> None:
    with pytest.raises(ValueError, match="end_offset"):
        ChunkRecord(
            chunk_id="c",
            tenant_id="t",
            corpus_id="c",
            document_id="d",
            document_version="v",
            chunk_type="child",
            content="x",
            content_hash="h",
            start_offset=50,
            end_offset=10,
            acl_policy_id="d",
            security_level="internal",
        )


def test_chunk_offset_consistency_both_null() -> None:
    chunk = ChunkRecord(
        chunk_id="c",
        tenant_id="t",
        corpus_id="c",
        document_id="d",
        document_version="v",
        chunk_type="child",
        content="x",
        content_hash="h",
        acl_policy_id="d",
        security_level="internal",
    )
    assert chunk.start_offset is None
    assert chunk.end_offset is None


def test_chunk_offset_consistency_one_null() -> None:
    with pytest.raises(ValueError, match="start_offset and end_offset must both"):
        ChunkRecord(
            chunk_id="c",
            tenant_id="t",
            corpus_id="c",
            document_id="d",
            document_version="v",
            chunk_type="child",
            content="x",
            content_hash="h",
            start_offset=10,
            acl_policy_id="d",
            security_level="internal",
        )


def test_chunk_effective_to_before_from() -> None:
    now = datetime(2026, 1, 1)
    later = datetime(2026, 6, 1)
    with pytest.raises(ValueError, match="effective_to"):
        ChunkRecord(
            chunk_id="c",
            tenant_id="t",
            corpus_id="c",
            document_id="d",
            document_version="v",
            chunk_type="child",
            content="x",
            content_hash="h",
            effective_from=later,
            effective_to=now,
            acl_policy_id="d",
            security_level="internal",
        )


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


def test_evidence_is_frozen() -> None:
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
    with pytest.raises(ValueError, match="frozen"):
        ev.text = "changed"  # type: ignore[misc]


def test_evidence_section_path_is_tuple() -> None:
    now = datetime(2026, 1, 1)
    ev = Evidence(
        evidence_id="e",
        tenant_id="t",
        corpus_id="c",
        document_id="d",
        document_version="v",
        source_uri="u",
        source_filename="f",
        text="x",
        text_hash="h",
        retrieval_query="q",
        authority_level=50,
        retrieved_at=now,
        acl_policy_id="d",
        policy_version="1",
        retrieval_iteration=1,
        section_path=("sec1", "sec2"),
    )
    assert ev.section_path == ("sec1", "sec2")
    assert isinstance(ev.section_path, tuple)
    with pytest.raises(AttributeError):
        ev.section_path.append("changed")  # type: ignore[attr-defined]


def test_evidence_authority_bounds() -> None:
    now = datetime(2026, 1, 1)
    with pytest.raises(ValueError, match="authority_level"):
        Evidence(
            evidence_id="e",
            tenant_id="t",
            corpus_id="c",
            document_id="d",
            document_version="v",
            source_uri="u",
            source_filename="f",
            text="x",
            text_hash="h",
            retrieval_query="q",
            authority_level=101,
            retrieved_at=now,
            acl_policy_id="d",
            policy_version="1",
            retrieval_iteration=1,
        )


# =============================================================================
# Ingestion lifecycle — document
# =============================================================================


def test_document_status_enum_values() -> None:
    assert DocumentStatus.DISCOVERED.value == "discovered"
    assert DocumentStatus.ACTIVE.value == "active"
    assert DocumentStatus.DELETED.value == "deleted"


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
# Ingestion lifecycle — job
# =============================================================================


def test_job_status_enum_values() -> None:
    assert JobStatus.QUEUED.value == "queued"
    assert JobStatus.SUCCEEDED.value == "succeeded"


def test_valid_job_transition_queued_to_running() -> None:
    assert valid_job_transition(JobStatus.QUEUED, JobStatus.RUNNING) is True


def test_valid_job_transition_running_to_succeeded() -> None:
    assert valid_job_transition(JobStatus.RUNNING, JobStatus.SUCCEEDED) is True


def test_valid_job_transition_running_to_cancelling() -> None:
    assert valid_job_transition(JobStatus.RUNNING, JobStatus.CANCELLING) is True


def test_valid_job_transition_cancelling_to_cancelled() -> None:
    assert valid_job_transition(JobStatus.CANCELLING, JobStatus.CANCELLED) is True


def test_valid_job_transition_succeeded_has_no_outgoing() -> None:
    assert valid_job_transition(JobStatus.SUCCEEDED, JobStatus.FAILED) is False


def test_valid_job_transition_cancelled_has_no_outgoing() -> None:
    assert valid_job_transition(JobStatus.CANCELLED, JobStatus.QUEUED) is False


def test_invalid_job_transition_queued_to_succeeded() -> None:
    assert valid_job_transition(JobStatus.QUEUED, JobStatus.SUCCEEDED) is False


def test_job_lifecycle_has_all_statuses() -> None:
    for status in JobStatus:
        assert status in JOB_LIFECYCLE


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
        tenant_id="t1",
        status=JobStatus.RUNNING,
        started_at=now,
        raw_hash="abc",
        parser_version="1.0",
        chunking_version="1.0",
        embedding_version="1.0",
    )
    assert manifest.job_id == "job-1"
    assert manifest.child_count == 0
    assert manifest.error_code is None


def test_ingestion_manifest_uses_enum() -> None:
    now = datetime(2026, 1, 1)
    manifest = IngestionManifest(
        job_id="j1",
        document_id="d1",
        document_version="v1",
        corpus_id="c1",
        tenant_id="t1",
        status=JobStatus.RUNNING,
        started_at=now,
        raw_hash="h",
        parser_version="1",
        chunking_version="1",
        embedding_version="1",
    )
    assert manifest.status == JobStatus.RUNNING


# =============================================================================
# Migration scaffolding — real SQLite execution
# =============================================================================


def _load_migration_script() -> str:
    import os

    path = os.path.join(os.path.dirname(__file__), "..", "migrations", "001_initial_schema.sql")
    assert os.path.isfile(path)
    with open(path) as f:
        return f.read()


def test_migration_creates_four_tables() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(_load_migration_script())
    cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    tables = {row[0] for row in cur.fetchall()}
    assert tables == {"corpus_registry", "documents", "ingestion_jobs", "chunks"}
    conn.close()


def test_migration_has_expected_indexes() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(_load_migration_script())

    cur = conn.execute("SELECT name FROM sqlite_master WHERE type='index' ORDER BY name")
    index_names = [row[0] for row in cur.fetchall() if not row[0].startswith("sqlite_autoindex")]

    assert "idx_documents_tenant_corpus" in index_names
    assert "idx_documents_corpus_status" in index_names
    assert "idx_ingestion_jobs_tenant" in index_names
    assert "idx_ingestion_jobs_document" in index_names
    assert "idx_chunks_tenant" in index_names
    assert "idx_documents_active_version" in index_names
    conn.close()


def test_migration_column_integrity() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(_load_migration_script())

    cur = conn.execute("PRAGMA table_info(documents)")
    doc_cols = {row[1] for row in cur.fetchall()}
    for col in (
        "allowed_user_ids",
        "allowed_group_ids",
        "denied_user_ids",
        "denied_group_ids",
        "acl_scope",
        "acl_policy_id",
        "authority_level",
        "status",
        "indexed_at",
        "deleted_at",
        "tenant_id",
        "corpus_id",
    ):
        assert col in doc_cols, f"documents missing column: {col}"

    cur = conn.execute("PRAGMA table_info(ingestion_jobs)")
    job_cols = {row[1] for row in cur.fetchall()}
    assert "tenant_id" in job_cols

    cur = conn.execute("PRAGMA table_info(corpus_registry)")
    reg_cols = {row[1] for row in cur.fetchall()}
    for col in ("capability_ids", "metadata_schema"):
        assert col in reg_cols, f"corpus_registry missing column: {col}"

    cur = conn.execute("PRAGMA table_info(chunks)")
    chunk_cols = {row[1] for row in cur.fetchall()}
    for col in ("allowed_user_ids", "acl_scope", "start_offset", "end_offset"):
        assert col in chunk_cols

    conn.close()


def test_migration_foreign_key_declarations() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(_load_migration_script())

    cur = conn.execute("PRAGMA foreign_key_list(documents)")
    doc_fks = cur.fetchall()
    assert len(doc_fks) == 2
    assert all(fk[2] == "corpus_registry" for fk in doc_fks)
    cols = {(fk[3], fk[4]) for fk in doc_fks}
    assert ("corpus_id", "corpus_id") in cols
    assert ("tenant_id", "tenant_id") in cols

    cur = conn.execute("PRAGMA foreign_key_list(ingestion_jobs)")
    job_fks = cur.fetchall()
    assert len(job_fks) == 4
    assert all(fk[2] == "documents" for fk in job_fks)

    cur = conn.execute("PRAGMA foreign_key_list(chunks)")
    chunk_fks = cur.fetchall()
    assert len(chunk_fks) == 4
    assert all(fk[2] == "documents" for fk in chunk_fks)

    conn.close()


def test_migration_fk_prevents_orphan_document() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(_load_migration_script())
    conn.execute("PRAGMA foreign_keys = ON")

    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        conn.execute(
            "INSERT INTO documents (document_id, tenant_id, corpus_id, source_uri, title, "
            "source_filename, mime_type, version, content_hash, status, acl_policy_id, "
            "security_level, parser_name, parser_version, chunking_version, "
            "embedding_model, embedding_version, discovered_at, last_synced_at) "
            "VALUES ('d','t','c','u','t','f','t','v','h','discovered','d',"
            "'internal','p','1','1','m','1','2026-01-01','2026-01-01')"
        )

    conn.close()


def test_migration_fk_allows_valid_document() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(_load_migration_script())
    conn.execute("PRAGMA foreign_keys = ON")

    conn.execute(
        "INSERT INTO corpus_registry (corpus_id, tenant_id, name, description, domain, owner, "
        "source_type, security_policy_id, default_security_level, created_at, updated_at) "
        "VALUES ('c','t','Test Corpus','desc','dom','o','documents','d','internal',"
        "'2026-01-01','2026-01-01')"
    )
    conn.execute(
        "INSERT INTO documents (document_id, tenant_id, corpus_id, source_uri, title, "
        "source_filename, mime_type, version, content_hash, status, acl_policy_id, "
        "security_level, parser_name, parser_version, chunking_version, "
        "embedding_model, embedding_version, discovered_at, last_synced_at) "
        "VALUES ('d','t','c','u','t','f','t','v','h','discovered','d',"
        "'internal','p','1','1','m','1','2026-01-01','2026-01-01')"
    )

    conn.close()


def test_migration_unique_active_version_index() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(_load_migration_script())
    conn.execute("PRAGMA foreign_keys = ON")

    conn.execute(
        "INSERT INTO corpus_registry (corpus_id, tenant_id, name, description, domain, owner, "
        "source_type, security_policy_id, default_security_level, created_at, updated_at) "
        "VALUES ('c','t','TC','d','o','o','documents','d','internal','2026-01-01','2026-01-01')"
    )
    conn.execute(
        "INSERT INTO documents (document_id, tenant_id, corpus_id, source_uri, title, "
        "source_filename, mime_type, version, content_hash, status, acl_policy_id, "
        "security_level, parser_name, parser_version, chunking_version, "
        "embedding_model, embedding_version, discovered_at, last_synced_at, indexed_at) "
        "VALUES ('d','t','c','u','t','f','t','v1','h','active','d',"
        "'internal','p','1','1','m','1','2026-01-01','2026-01-01','2026-01-01')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO documents (document_id, tenant_id, corpus_id, source_uri, title, "
            "source_filename, mime_type, version, content_hash, status, acl_policy_id, "
            "security_level, parser_name, parser_version, chunking_version, "
            "embedding_model, embedding_version, discovered_at, last_synced_at, indexed_at) "
            "VALUES ('d','t','c','u','t','f','t','v2','h','active','d',"
            "'internal','p','1','1','m','1','2026-01-01','2026-01-01','2026-01-01')"
        )

    conn.close()


def test_migration_document_authority_check() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(_load_migration_script())
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        "INSERT INTO corpus_registry (corpus_id, tenant_id, name, description, domain, owner, "
        "source_type, security_policy_id, default_security_level, created_at, updated_at) "
        "VALUES ('c','t','TC','d','o','o','documents','d','internal','2026-01-01','2026-01-01')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO documents (document_id, tenant_id, corpus_id, source_uri, title, "
            "source_filename, mime_type, version, content_hash, status, acl_policy_id, "
            "security_level, authority_level, parser_name, parser_version, chunking_version, "
            "embedding_model, embedding_version, discovered_at, last_synced_at) "
            "VALUES ('d','t','c','u','t','f','t','v','h','discovered','d',"
            "'internal',150,'p','1','1','m','1','2026-01-01','2026-01-01')"
        )
    conn.close()


def test_migration_document_acl_scope_check() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(_load_migration_script())
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        "INSERT INTO corpus_registry (corpus_id, tenant_id, name, description, domain, owner, "
        "source_type, security_policy_id, default_security_level, created_at, updated_at) "
        "VALUES ('c','t','TC','d','o','o','documents','d','internal','2026-01-01','2026-01-01')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO documents (document_id, tenant_id, corpus_id, source_uri, title, "
            "source_filename, mime_type, version, content_hash, status, acl_policy_id, "
            "security_level, acl_scope, parser_name, parser_version, chunking_version, "
            "embedding_model, embedding_version, discovered_at, last_synced_at) "
            "VALUES ('d','t','c','u','t','f','t','v','h','discovered','d',"
            "'internal','public','p','1','1','m','1','2026-01-01','2026-01-01')"
        )
    conn.close()


def test_migration_document_status_check() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(_load_migration_script())
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        "INSERT INTO corpus_registry (corpus_id, tenant_id, name, description, domain, owner, "
        "source_type, security_policy_id, default_security_level, created_at, updated_at) "
        "VALUES ('c','t','TC','d','o','o','documents','d','internal','2026-01-01','2026-01-01')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO documents (document_id, tenant_id, corpus_id, source_uri, title, "
            "source_filename, mime_type, version, content_hash, status, acl_policy_id, "
            "security_level, parser_name, parser_version, chunking_version, "
            "embedding_model, embedding_version, discovered_at, last_synced_at) "
            "VALUES ('d','t','c','u','t','f','t','v','h','archived','d',"
            "'internal','p','1','1','m','1','2026-01-01','2026-01-01')"
        )
    conn.close()


def test_migration_document_active_needs_indexed_at() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(_load_migration_script())
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        "INSERT INTO corpus_registry (corpus_id, tenant_id, name, description, domain, owner, "
        "source_type, security_policy_id, default_security_level, created_at, updated_at) "
        "VALUES ('c','t','TC','d','o','o','documents','d','internal','2026-01-01','2026-01-01')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO documents (document_id, tenant_id, corpus_id, source_uri, title, "
            "source_filename, mime_type, version, content_hash, status, acl_policy_id, "
            "security_level, parser_name, parser_version, chunking_version, "
            "embedding_model, embedding_version, discovered_at, last_synced_at) "
            "VALUES ('d','t','c','u','t','f','t','v','h','active','d',"
            "'internal','p','1','1','m','1','2026-01-01','2026-01-01')"
        )
    conn.close()


def test_migration_job_status_check() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(_load_migration_script())
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        "INSERT INTO corpus_registry (corpus_id, tenant_id, name, description, domain, owner, "
        "source_type, security_policy_id, default_security_level, created_at, updated_at) "
        "VALUES ('c','t','TC','d','o','o','documents','d','internal','2026-01-01','2026-01-01')"
    )
    conn.execute(
        "INSERT INTO documents (document_id, tenant_id, corpus_id, source_uri, title, "
        "source_filename, mime_type, version, content_hash, status, acl_policy_id, "
        "security_level, parser_name, parser_version, chunking_version, "
        "embedding_model, embedding_version, discovered_at, last_synced_at) "
        "VALUES ('d','t','c','u','t','f','t','v','h','discovered','d',"
        "'internal','p','1','1','m','1','2026-01-01','2026-01-01')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO ingestion_jobs (job_id, document_id, document_version, corpus_id, "
            "tenant_id, status, started_at, raw_hash, parser_version, chunking_version, "
            "embedding_version) "
            "VALUES ('j','d','v','c','t','pending','2026-01-01','h','1','1','1')"
        )
    conn.close()


# =============================================================================
# Migration — FK enforcement (real insertions)
# =============================================================================


def _seed_corpus_doc(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO corpus_registry (corpus_id, tenant_id, name, description, domain, owner, "
        "source_type, security_policy_id, default_security_level, created_at, updated_at) "
        "VALUES ('c','t','TC','d','o','o','documents','d','internal','2026-01-01','2026-01-01')"
    )
    conn.execute(
        "INSERT INTO documents (document_id, tenant_id, corpus_id, source_uri, title, "
        "source_filename, mime_type, version, content_hash, status, acl_policy_id, "
        "security_level, parser_name, parser_version, chunking_version, "
        "embedding_model, embedding_version, discovered_at, last_synced_at) "
        "VALUES ('d','t','c','u','t','f','t','v','h','discovered','d',"
        "'internal','p','1','1','m','1','2026-01-01','2026-01-01')"
    )


@pytest.mark.parametrize(
    ("job_id", "doc_id", "doc_ver", "corpus", "tenant"),
    [
        ("j1", "d", "v", "c", "WRONG"),  # bad tenant
        ("j2", "NODOC", "v", "c", "t"),  # bad document_id
        ("j3", "d", "BADVER", "c", "t"),  # bad version
    ],
)
def test_migration_ingestion_job_fk_enforced(
    job_id: str,
    doc_id: str,
    doc_ver: str,
    corpus: str,
    tenant: str,
) -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(_load_migration_script())
    conn.execute("PRAGMA foreign_keys = ON")
    _seed_corpus_doc(conn)
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        conn.execute(
            "INSERT INTO ingestion_jobs (job_id, document_id, document_version, corpus_id, "
            "tenant_id, status, started_at, raw_hash, parser_version, chunking_version, "
            "embedding_version) VALUES (?,?,?,?,?,'queued','2026-01-01','h','1','1','1')",
            (job_id, doc_id, doc_ver, corpus, tenant),
        )
    conn.close()


def test_migration_chunk_fk_enforced() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(_load_migration_script())
    conn.execute("PRAGMA foreign_keys = ON")
    _seed_corpus_doc(conn)
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        conn.execute(
            "INSERT INTO chunks (chunk_id, tenant_id, corpus_id, document_id, document_version, "
            "chunk_type, content, content_hash, acl_policy_id, security_level) "
            "VALUES ('c','t','c','NOEXIST','v','child','x','h','d','internal')"
        )
    conn.close()


def test_migration_chunk_fk_accepted() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(_load_migration_script())
    conn.execute("PRAGMA foreign_keys = ON")
    _seed_corpus_doc(conn)
    conn.execute(
        "INSERT INTO chunks (chunk_id, tenant_id, corpus_id, document_id, document_version, "
        "chunk_type, content, content_hash, acl_policy_id, security_level) "
        "VALUES ('c','t','c','d','v','child','x','h','d','internal')"
    )
    conn.close()


# =============================================================================
# Migration — chunk CHECK constraints (real insertions)
# =============================================================================

_CHUNK_FIXED_COLS = (
    "chunk_id, tenant_id, corpus_id, document_id, document_version, "
    "content, content_hash, acl_policy_id, security_level"
)
_CHUNK_FIXED_VALS = "'c','t','c','d','v','x','h','d','internal'"


@pytest.mark.parametrize(
    ("chunk_type_val", "extra_cols", "extra_vals", "desc"),
    [
        ("'invalid'", "", "", "chunk_type not parent/child"),
        ("'child'", ", authority_level", ",150", "authority outside 0-100"),
        ("'child'", ", acl_scope", ",'public'", "acl_scope not tenant/restricted"),
        ("'child'", ", start_offset", ",10", "start without end"),
        ("'child'", ", end_offset", ",10", "end without start"),
        ("'child'", ", start_offset, end_offset", ",-1,0", "negative start"),
        ("'child'", ", start_offset, end_offset", ",0,-1", "negative end"),
        ("'child'", ", start_offset, end_offset", ",50,10", "end < start"),
    ],
)
def test_migration_chunk_check_constraints(
    chunk_type_val: str,
    extra_cols: str,
    extra_vals: str,
    desc: str,
) -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(_load_migration_script())
    conn.execute("PRAGMA foreign_keys = ON")
    _seed_corpus_doc(conn)
    sql = (
        f"INSERT INTO chunks ({_CHUNK_FIXED_COLS}, chunk_type{extra_cols}) "
        f"VALUES ({_CHUNK_FIXED_VALS}, {chunk_type_val}{extra_vals})"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(sql)
    conn.close()


def test_migration_chunk_check_valid_offset_pass() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(_load_migration_script())
    conn.execute("PRAGMA foreign_keys = ON")
    _seed_corpus_doc(conn)
    conn.execute(
        f"INSERT INTO chunks ({_CHUNK_FIXED_COLS}, chunk_type, start_offset, end_offset) "
        f"VALUES ({_CHUNK_FIXED_VALS}, 'child', 0, 100)"
    )
    conn.close()


def test_migration_has_correct_bootstrap_comment() -> None:
    content = _load_migration_script()
    assert "during first uv sync" not in content
    assert "bootstrap command" in content
