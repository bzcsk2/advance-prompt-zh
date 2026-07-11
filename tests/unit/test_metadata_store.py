"""Unit tests for MetadataStore (ingestion control-plane source of truth)."""

import os
import tempfile
from datetime import datetime, timezone

from agentic_rag_enterprise.domain.document import SourceDocument
from agentic_rag_enterprise.domain.ingestion import DocumentStatus
from agentic_rag_enterprise.storage.metadata_store import (
    ActiveVersionConflict,
    MetadataStore,
)

_FIXED = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def _tmp_db_path() -> str:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    return path


def _seed_corpus(store: MetadataStore, tenant_id: str = "t1", corpus_id: str = "eng") -> None:
    """Insert a minimal corpus_registry row (FK parent of ``documents``)."""
    store._conn.execute(  # noqa: SLF001 - test helper reaches into raw conn
        """
        INSERT INTO corpus_registry (
            corpus_id, tenant_id, name, description, created_at, updated_at
        ) VALUES (?, ?, 'corpus', '', ?, ?)
        """,
        (corpus_id, tenant_id, _FIXED.isoformat(), _FIXED.isoformat()),
    )


def _make_doc(
    *,
    tenant_id: str = "t1",
    corpus_id: str = "eng",
    document_id: str = "d1",
    version: str = "v1",
    status: DocumentStatus = DocumentStatus.PROCESSING,
    security_level: str = "public",
    acl_scope: str = "tenant",
) -> SourceDocument:
    return SourceDocument(
        document_id=document_id,
        tenant_id=tenant_id,
        corpus_id=corpus_id,
        source_uri=f"inline://{document_id}",
        source_connector="file",
        title=document_id,
        source_filename=f"{document_id}.md",
        mime_type="text/markdown",
        version=version,
        content_hash="abc",
        status=status,
        authority_level=50,
        deprecated=False,
        acl_policy_id="default",
        security_level=security_level,
        acl_scope=acl_scope,  # type: ignore[arg-type]
        allowed_user_ids=["u1"],
        allowed_group_ids=["g1"],
        denied_user_ids=[],
        denied_group_ids=[],
        parser_name="markdown",
        parser_version="1.0",
        chunking_version="1.0",
        embedding_model="fake",
        embedding_version="1.0",
        discovered_at=_FIXED,
        indexed_at=_FIXED if status == DocumentStatus.ACTIVE else None,
        last_synced_at=_FIXED,
    )


def test_migrations_create_schema_and_are_idempotent() -> None:
    path = _tmp_db_path()
    store = MetadataStore(path)
    # Re-opening applies migrations again without error (idempotent).
    store.close()
    store2 = MetadataStore(path)
    assert store2.get_document("t1", "eng", "d1", "v1") is None
    store2.close()
    os.unlink(path)


def test_document_roundtrip_preserves_json_and_dates() -> None:
    path = _tmp_db_path()
    store = MetadataStore(path)
    _seed_corpus(store)
    doc = _make_doc(security_level="internal")
    store.upsert_document(doc)
    got = store.get_document("t1", "eng", "d1", "v1")
    assert got is not None
    assert got.security_level == "internal"
    assert got.status == DocumentStatus.PROCESSING
    assert got.allowed_user_ids == ["u1"]
    assert got.allowed_group_ids == ["g1"]
    assert got.tenant_id == "t1" and got.corpus_id == "eng"
    store.close()
    os.unlink(path)


def test_unique_document_version_is_upsert_not_duplicate() -> None:
    path = _tmp_db_path()
    store = MetadataStore(path)
    _seed_corpus(store)
    store.upsert_document(_make_doc())
    store.upsert_document(_make_doc(security_level="internal"))
    again = store.get_document("t1", "eng", "d1", "v1")
    assert again is not None
    assert again.security_level == "internal"  # updated, not duplicated
    store.close()
    os.unlink(path)


def test_get_active_document_only_sees_active() -> None:
    path = _tmp_db_path()
    store = MetadataStore(path)
    _seed_corpus(store)
    assert store.get_active_document("t1", "eng", "d1") is None
    store.upsert_document(_make_doc(status=DocumentStatus.ACTIVE))
    active = store.get_active_document("t1", "eng", "d1")
    assert active is not None and active.version == "v1"
    store.close()
    os.unlink(path)


def test_commit_active_version_switches_and_increments_revision() -> None:
    path = _tmp_db_path()
    store = MetadataStore(path)
    _seed_corpus(store)
    store.upsert_document(_make_doc(version="v1", status=DocumentStatus.PROCESSING))
    store.upsert_document(_make_doc(version="v2", status=DocumentStatus.PROCESSING))

    rev1 = store.commit_active_version(
        tenant_id="t1",
        corpus_id="eng",
        document_id="d1",
        new_version="v1",
        expected_revision=0,
    )
    assert rev1 == 1
    assert store.get_active_document("t1", "eng", "d1").version == "v1"
    assert store.get_current_revision("t1", "eng", "d1") == 1

    rev2 = store.commit_active_version(
        tenant_id="t1",
        corpus_id="eng",
        document_id="d1",
        new_version="v2",
        expected_revision=1,
    )
    assert rev2 == 2
    active = store.get_active_document("t1", "eng", "d1")
    assert active.version == "v2"
    # Old version is superseded (deprecated + non-active), not retrieved.
    old = store.get_document("t1", "eng", "d1", "v1")
    assert old is not None
    assert old.status == DocumentStatus.DEPRECATED
    assert old.deprecated is True
    assert store.get_current_revision("t1", "eng", "d1") == 2
    store.close()
    os.unlink(path)


def test_commit_active_version_rejects_stale_revision() -> None:
    path = _tmp_db_path()
    store = MetadataStore(path)
    _seed_corpus(store)
    store.upsert_document(_make_doc(version="v1", status=DocumentStatus.PROCESSING))
    store.upsert_document(_make_doc(version="v2", status=DocumentStatus.PROCESSING))
    store.commit_active_version(
        tenant_id="t1",
        corpus_id="eng",
        document_id="d1",
        new_version="v1",
        expected_revision=0,
    )
    # A competing commit using the stale expected_revision=0 must fail closed.
    import pytest

    with pytest.raises(ActiveVersionConflict):
        store.commit_active_version(
            tenant_id="t1",
            corpus_id="eng",
            document_id="d1",
            new_version="v2",
            expected_revision=0,
        )
    # But the correct revision proceeds.
    rev = store.commit_active_version(
        tenant_id="t1",
        corpus_id="eng",
        document_id="d1",
        new_version="v2",
        expected_revision=1,
    )
    assert rev == 2
    store.close()
    os.unlink(path)


def test_step_markers_are_reentrant() -> None:
    path = _tmp_db_path()
    store = MetadataStore(path)
    _seed_corpus(store)
    # A job row must exist before step markers (FK to ingestion_jobs).
    store.upsert_document(_make_doc(status=DocumentStatus.PROCESSING))
    store.acquire_job(
        job_id="job-1",
        document_id="d1",
        document_version="v1",
        corpus_id="eng",
        tenant_id="t1",
        parser_version="1.0",
        chunking_version="1.0",
        embedding_version="1.0",
        raw_hash="abc",
    )
    # acquire_job already marks the "acquire" step.
    assert store.is_step_done("job-1", "acquire")
    store.mark_step("job-1", "write_qdrant", "done")
    assert store.is_step_done("job-1", "write_qdrant")
    assert not store.is_step_done("job-1", "commit")
    assert store.list_done_steps("job-1") == ["acquire", "write_qdrant"]
    # Re-marking is idempotent.
    store.mark_step("job-1", "acquire", "done")
    assert store.list_done_steps("job-1") == ["acquire", "write_qdrant"]
    store.close()
    os.unlink(path)
