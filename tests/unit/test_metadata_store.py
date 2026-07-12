"""Unit tests for MetadataStore (ingestion control-plane source of truth)."""

import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from agentic_rag_enterprise.domain.document import SourceDocument
from agentic_rag_enterprise.domain.ingestion import (
    DocumentStatus,
    IngestionManifest,
    JobStatus,
)
import pytest
import threading

from agentic_rag_enterprise.storage import metadata_store as ms
from agentic_rag_enterprise.storage.metadata_store import (
    ActiveVersionConflict,
    BuildConflict,
    JobIdentityConflict,
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

    rev1, prev1 = store.commit_active_version(
        tenant_id="t1",
        corpus_id="eng",
        document_id="d1",
        new_version="v1",
        expected_revision=0,
    )
    assert rev1 == 1
    assert prev1 is None  # no prior active version
    assert store.get_active_document("t1", "eng", "d1").version == "v1"
    assert store.get_current_revision("t1", "eng", "d1") == 1

    rev2, prev2 = store.commit_active_version(
        tenant_id="t1",
        corpus_id="eng",
        document_id="d1",
        new_version="v2",
        expected_revision=1,
    )
    assert rev2 == 2
    assert prev2 == "v1"  # the version actually replaced
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
    rev, _ = store.commit_active_version(
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
        base_revision=0,
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


def test_current_revision_is_monotonic_over_all_versions() -> None:
    # After the active version is deprecated (no active row), the revision must
    # still reflect the maximum ever seen, not fall back to 0 (build plan
    # §10.10 #8, E-008.1 P1-3).
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
    store.commit_active_version(
        tenant_id="t1",
        corpus_id="eng",
        document_id="d1",
        new_version="v2",
        expected_revision=1,
    )
    assert store.get_active_document("t1", "eng", "d1").version == "v2"
    assert store.get_current_revision("t1", "eng", "d1") == 2
    store.close()
    os.unlink(path)


def test_job_identity_is_immutable() -> None:
    path = _tmp_db_path()
    store = MetadataStore(path)
    _seed_corpus(store)
    store.upsert_document(_make_doc(status=DocumentStatus.PROCESSING))
    store.acquire_job(
        job_id="j1",
        document_id="d1",
        document_version="v1",
        corpus_id="eng",
        tenant_id="t1",
        parser_version="1.0",
        chunking_version="1.0",
        embedding_version="1.0",
        raw_hash="abc",
        base_revision=0,
    )
    # Same identity -> ok.
    store.validate_job_identity(
        job_id="j1",
        tenant_id="t1",
        corpus_id="eng",
        document_id="d1",
        document_version="v1",
        raw_hash="abc",
    )
    # Different version bound to same job_id -> conflict (E-008.1 P1-6).
    import pytest

    with pytest.raises(JobIdentityConflict):
        store.validate_job_identity(
            job_id="j1",
            tenant_id="t1",
            corpus_id="eng",
            document_id="d1",
            document_version="v2",
            raw_hash="abc",
        )
    store.close()
    os.unlink(path)


def test_job_manifest_is_persisted() -> None:
    path = _tmp_db_path()
    store = MetadataStore(path)
    _seed_corpus(store)
    store.upsert_document(_make_doc(status=DocumentStatus.PROCESSING))
    store.acquire_job(
        job_id="j1",
        document_id="d1",
        document_version="v1",
        corpus_id="eng",
        tenant_id="t1",
        parser_version="1.0",
        chunking_version="1.0",
        embedding_version="1.0",
        raw_hash="abc",
        base_revision=0,
    )
    manifest = IngestionManifest(
        job_id="j1",
        document_id="d1",
        document_version="v1",
        corpus_id="eng",
        tenant_id="t1",
        status=JobStatus.SUCCEEDED,
        started_at=_FIXED,
        raw_hash="abc",
        parent_count=2,
        child_count=5,
        parser_version="1.0",
        chunking_version="1.0",
        embedding_version="1.0",
    )
    store.set_job_manifest("j1", manifest.model_dump_json())
    row = store._conn.execute("SELECT manifest FROM ingestion_jobs WHERE job_id='j1'").fetchone()
    assert row["manifest"]
    assert IngestionManifest.model_validate_json(row["manifest"]).job_id == "j1"
    store.close()
    os.unlink(path)


def test_migration_atomicity_rolls_back_on_failure(tmp_path) -> None:
    # P1-6: a crash between DDL and the schema_migrations marker must roll back
    # the whole migration, leaving no partial schema and no marker (so the next
    # boot re-applies cleanly instead of hitting a duplicate-column error).
    import agentic_rag_enterprise.storage.metadata_store as ms

    bad_dir = tmp_path / "migrations"
    bad_dir.mkdir()
    (bad_dir / "001_bad.sql").write_text(
        "CREATE TABLE good_table (id INTEGER PRIMARY KEY);\n"
        "THIS IS NOT VALID SQL;\n"
    )
    db = tmp_path / "md.db"
    orig = ms.MIGRATIONS_DIR
    ms.MIGRATIONS_DIR = bad_dir
    try:
        # The bad migration raises; its DDL must be rolled back.
        try:
            MetadataStore(str(db))
            raise AssertionError("expected migration failure")
        except Exception:
            pass
    finally:
        ms.MIGRATIONS_DIR = orig

    # Reopen with the real migrations: good_table must not exist, only the
    # schema_migrations bookkeeping table was created (outside the transaction).
    store = MetadataStore(str(db))
    tables = {
        r["name"]
        for r in store._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert "good_table" not in tables
    assert "schema_migrations" in tables
    store.close()


def test_build_lease_serializes_concurrent_in_flight_builds() -> None:
    # P1-3 / P1-4: a concurrent in-flight build for the same
    # (tenant, corpus, document, version) is rejected with BuildConflict, so it
    # cannot race on the shared (deterministic-ID) data plane. Two independent
    # connections to the same DB file exercise the real BEGIN IMMEDIATE
    # serialization at the engine level.
    path = _tmp_db_path()
    bootstrap = MetadataStore(path)
    _seed_corpus(bootstrap)
    bootstrap.upsert_document(_make_doc(status=DocumentStatus.PROCESSING))
    bootstrap.close()

    results: dict[str, str] = {}

    def run(job_id: str) -> None:
        store = MetadataStore(path)
        try:
            store.acquire_job(
                job_id=job_id,
                document_id="d1",
                document_version="v1",
                corpus_id="eng",
                tenant_id="t1",
                parser_version="1.0",
                chunking_version="1.0",
                embedding_version="1.0",
                raw_hash="abc",
                base_revision=0,
            )
            results[job_id] = "ok"
        except BuildConflict:
            results[job_id] = "conflict"
        finally:
            store.close()

    t_a = threading.Thread(target=run, args=("a",))
    t_b = threading.Thread(target=run, args=("b",))
    t_a.start()
    t_b.start()
    t_a.join()
    t_b.join()

    # Exactly one build owns the lease; the other is rejected.
    owners = [k for k, v in results.items() if v == "ok"]
    conflicts = [k for k, v in results.items() if v == "conflict"]
    assert len(owners) == 1
    assert len(conflicts) == 1
    verify = MetadataStore(path)
    assert verify.get_build_owner("t1", "eng", "d1", "v1") == owners[0]
    verify.close()
    os.unlink(path)


def test_build_lease_takeover_after_failed_owner() -> None:
    # P1-3 / P1-4: when the lease owner's job has already terminated (failed),
    # a re-delivered job takes over the lease and rebuilds (no conflict).
    path = _tmp_db_path()
    store = MetadataStore(path)
    _seed_corpus(store)
    store.upsert_document(_make_doc(status=DocumentStatus.PROCESSING))
    store.acquire_job(
        job_id="first",
        document_id="d1",
        document_version="v1",
        corpus_id="eng",
        tenant_id="t1",
        parser_version="1.0",
        chunking_version="1.0",
        embedding_version="1.0",
        raw_hash="abc",
        base_revision=0,
    )
    # First build failed.
    store.mark_job_terminal("first", JobStatus.FAILED)
    # A new delivery takes over the lease (reassignment, not conflict).
    status, _generation = store.acquire_job(
        job_id="second",
        document_id="d1",
        document_version="v1",
        corpus_id="eng",
        tenant_id="t1",
        parser_version="1.0",
        chunking_version="1.0",
        embedding_version="1.0",
        raw_hash="abc",
        base_revision=0,
    )
    assert status == JobStatus.RUNNING
    assert store.get_build_owner("t1", "eng", "d1", "v1") == "second"
    # Takeover advanced the fencing token so the original (failed) owner's
    # generation no longer matches the live lease (E-008.3 P1-2).
    assert store.get_lease_generation("t1", "eng", "d1", "v1") >= 2
    store.close()
    os.unlink(path)


def test_build_attempt_rejects_duplicate_execution_for_same_job_id() -> None:
    # E-008.4 P1-3 (DB-level execution attempt): a second live execution
    # attempt for the SAME job_id on a RUNNING lease (e.g. a second process
    # that re-delivered the same job_id) is a duplicate delivery, NOT a
    # recovery: it is rejected with BuildConflict and does NOT advance the
    # fencing generation, so the in-flight attempt keeps its authority over the
    # deterministic data plane. An explicit recovery (recover=True) is allowed
    # to advance the generation.
    path = _tmp_db_path()
    store = MetadataStore(path)
    _seed_corpus(store)
    store.upsert_document(_make_doc(status=DocumentStatus.PROCESSING))
    # First (live) attempt claims the lease.
    store.acquire_job(
        job_id="j1",
        document_id="d1",
        document_version="v1",
        corpus_id="eng",
        tenant_id="t1",
        parser_version="1.0",
        chunking_version="1.0",
        embedding_version="1.0",
        raw_hash="abc",
        base_revision=0,
        attempt_id="attempt-aaa",
    )
    # Second live attempt for the SAME job_id but a DIFFERENT attempt_id
    # (cross-process re-delivery) on the still-RUNNING lease -> duplicate
    # -> BuildConflict, generation unchanged.
    dup = MetadataStore(path)
    with pytest.raises(BuildConflict):
        dup.acquire_job(
            job_id="j1",
            document_id="d1",
            document_version="v1",
            corpus_id="eng",
            tenant_id="t1",
            parser_version="1.0",
            chunking_version="1.0",
            embedding_version="1.0",
            raw_hash="abc",
            base_revision=0,
            attempt_id="attempt-bbb",
        )
    assert store.get_lease_generation("t1", "eng", "d1", "v1") == 1
    dup.close()

    # Explicit recovery of the (crashed) attempt advances the generation.
    rec = MetadataStore(path)
    status, generation = rec.acquire_job(
        job_id="j1",
        document_id="d1",
        document_version="v1",
        corpus_id="eng",
        tenant_id="t1",
        parser_version="1.0",
        chunking_version="1.0",
        embedding_version="1.0",
        raw_hash="abc",
        base_revision=0,
        attempt_id="attempt-ccc",
        recover=True,
    )
    assert status == JobStatus.RUNNING
    assert generation == 2
    rec.close()
    store.close()
    os.unlink(path)


def _broken_579d729_migrations_dir() -> Path:
    # A migrations dir holding ONLY 001..007 (the state after deploying the
    # prior closure commit, where 006 was ALTER-only and the backfill had NOT
    # yet run). Used to reproduce the real upgrade path where 006 is already
    # recorded as applied and therefore SKIPPED.
    work = Path(tempfile.mkdtemp()) / "migrations"
    work.mkdir()
    for p in sorted(ms.MIGRATIONS_DIR.glob("*.sql")):
        if p.stem < "008":
            shutil.copy(p, work / p.name)
    return work


def test_migration_008_backfills_on_real_upgrade_from_deployed_026190f() -> None:
    # E-008.4 P1-2 (upgrade path): a database that ALREADY deployed the prior
    # closure commit (migration 006 recorded as applied) must still get the
    # previous_active_version backfill when upgraded to the fixed build. The
    # backfill therefore lives in a NEW migration 008, not inside the already-
    # published 006 (which the migrator skips).
    path = _tmp_db_path()
    work = _broken_579d729_migrations_dir()  # 006 already "deployed", no backfill
    orig = ms.MIGRATIONS_DIR
    ms.MIGRATIONS_DIR = work
    try:
        # Builds schema via 001..007 (006 = ALTER only, no backfill).
        store = MetadataStore(path)
    finally:
        ms.MIGRATIONS_DIR = orig

    _seed_corpus(store)
    store.upsert_document(_make_doc(version="v0", status=DocumentStatus.ACTIVE))
    store.upsert_document(_make_doc(version="v1", status=DocumentStatus.PROCESSING))
    # Legacy E-008.3 lease owned by jA (claimed before 006): column is NULL and
    # the replaced version is recorded only on the JOBS row (as 006 captured it).
    store._conn.execute(  # noqa: SLF001
        "INSERT INTO ingestion_jobs "
        "(job_id, document_id, document_version, corpus_id, tenant_id, status, "
        " started_at, raw_hash, parent_count, child_count, parser_version, "
        " chunking_version, embedding_version, base_revision, previous_active_version) "
        "VALUES ('jA','d1','v1','eng','t1','failed','','',0,0,'','','',1,'v0')"
    )
    store._conn.execute(  # noqa: SLF001
        "INSERT INTO document_builds "
        "(tenant_id, corpus_id, document_id, document_version, owner_job_id, "
        " status, base_revision, acquired_at, lease_generation) "
        "VALUES ('t1','eng','d1','v1','jA','failed',1,'x',1)"
    )
    store._conn.commit()

    # The FIXED upgrade: applying migrations from the real dir (now incl. 008).
    # 006 is already recorded as applied -> SKIPPED; 008 runs the backfill.
    store.apply_migrations()
    row = store._conn.execute(  # noqa: SLF001
        "SELECT previous_active_version FROM document_builds WHERE owner_job_id='jA'"
    ).fetchone()
    assert row["previous_active_version"] == "v0"

    # A takeover AFTER upgrade inherits the true replaced version (not recomputed).
    take = MetadataStore(path)
    take.acquire_job(
        job_id="jB",
        document_id="d1",
        document_version="v1",
        corpus_id="eng",
        tenant_id="t1",
        parser_version="1.0",
        chunking_version="1.0",
        embedding_version="1.0",
        raw_hash="abc",
        base_revision=0,
        document=_make_doc(version="v1", status=DocumentStatus.PROCESSING),
    )
    assert take.get_job_previous_version("jB") == "v0"
    take.close()
    store.close()
    os.unlink(path)


def test_acquire_resumes_terminal_succeeded_lease_without_recover() -> None:
    # E-008.4 P1-3 terminal semantics: a same-job_id re-acquire on a TERMINAL
    # lease (succeeded -> build-lease status 'done') is a safe recovery and must
    # NOT raise (the build-lease status vocabulary is 'done'/'failed', NOT the
    # JobStatus enum). It resumes without recover=True.
    path = _tmp_db_path()
    store = MetadataStore(path)
    _seed_corpus(store)
    store.upsert_document(_make_doc(version="v1", status=DocumentStatus.PROCESSING))
    store.acquire_job(
        job_id="j1",
        document_id="d1",
        document_version="v1",
        corpus_id="eng",
        tenant_id="t1",
        parser_version="1.0",
        chunking_version="1.0",
        embedding_version="1.0",
        raw_hash="abc",
        base_revision=0,
        attempt_id="attempt-aaa",
        document=_make_doc(version="v1", status=DocumentStatus.PROCESSING),
    )
    # Succeeded job -> build-lease status is 'done' (not a JobStatus member).
    store.mark_job_terminal("j1", JobStatus.SUCCEEDED)
    # Same job_id, different attempt, NO recover -> terminal lease resumes safely.
    status, generation = store.acquire_job(
        job_id="j1",
        document_id="d1",
        document_version="v1",
        corpus_id="eng",
        tenant_id="t1",
        parser_version="1.0",
        chunking_version="1.0",
        embedding_version="1.0",
        raw_hash="abc",
        base_revision=0,
        attempt_id="attempt-bbb",
    )
    assert status == JobStatus.RUNNING
    assert generation == 2
    store.close()
    os.unlink(path)


# --- E-010: document mutation control-plane API ---------------------------------


def test_set_document_status_flips_and_records_deleted_at() -> None:
    path = _tmp_db_path()
    store = MetadataStore(path)
    _seed_corpus(store)
    store.upsert_document(_make_doc(version="v1", status=DocumentStatus.ACTIVE))
    store.set_document_status(
        "t1", "eng", "d1", "v1", DocumentStatus.DELETED, deleted_at=_FIXED
    )
    doc = store.get_document("t1", "eng", "d1", "v1")
    assert doc is not None
    assert doc.status == DocumentStatus.DELETED
    assert doc.deleted_at == _FIXED
    store.close()
    os.unlink(path)


def test_set_document_status_idempotent() -> None:
    path = _tmp_db_path()
    store = MetadataStore(path)
    _seed_corpus(store)
    store.upsert_document(_make_doc(version="v1", status=DocumentStatus.ACTIVE))
    store.set_document_status("t1", "eng", "d1", "v1", DocumentStatus.DELETED, deleted_at=_FIXED)
    # Re-asserting the same status must not raise and must keep the row deleted.
    store.set_document_status("t1", "eng", "d1", "v1", DocumentStatus.DELETED, deleted_at=_FIXED)
    assert store.get_document("t1", "eng", "d1", "v1").status == DocumentStatus.DELETED
    store.close()
    os.unlink(path)


def test_list_document_versions_and_get_document_latest() -> None:
    path = _tmp_db_path()
    store = MetadataStore(path)
    _seed_corpus(store)
    store.upsert_document(_make_doc(version="v1", status=DocumentStatus.ACTIVE))
    store.upsert_document(_make_doc(version="v2", status=DocumentStatus.DEPRECATED))
    assert set(store.list_document_versions("t1", "eng", "d1")) == {"v1", "v2"}
    latest = store.get_document_latest("t1", "eng", "d1")
    assert latest is not None
    assert latest.version in {"v1", "v2"}
    store.close()
    os.unlink(path)


def test_delete_document_removes_all_versions_and_is_idempotent() -> None:
    path = _tmp_db_path()
    store = MetadataStore(path)
    _seed_corpus(store)
    store.upsert_document(_make_doc(version="v1", status=DocumentStatus.ACTIVE))
    store.upsert_document(_make_doc(version="v2", status=DocumentStatus.DEPRECATED))
    store.delete_document("t1", "eng", "d1")
    assert store.get_document("t1", "eng", "d1", "v1") is None
    assert store.get_document("t1", "eng", "d1", "v2") is None
    # Purging an already-absent document is a no-op.
    store.delete_document("t1", "eng", "d1")
    store.close()
    os.unlink(path)


def test_update_document_acl_preserves_lifecycle_revision() -> None:
    path = _tmp_db_path()
    store = MetadataStore(path)
    _seed_corpus(store)
    store.upsert_document(_make_doc(version="v1", status=DocumentStatus.ACTIVE))
    rev_before = store._conn.execute(  # noqa: SLF001
        "SELECT lifecycle_revision FROM documents WHERE tenant_id='t1' AND corpus_id='eng' "
        "AND document_id='d1' AND version='v1'"
    ).fetchone()["lifecycle_revision"]
    store.update_document_acl(
        "t1", "eng", "d1", "v1",
        security_level="secret",
        acl_scope="restricted",
        allowed_user_ids=["u9"],
        allowed_group_ids=[],
        denied_user_ids=[],
        denied_group_ids=[],
    )
    row = store._conn.execute(  # noqa: SLF001
        "SELECT lifecycle_revision, security_level, acl_scope, allowed_user_ids "
        "FROM documents WHERE tenant_id='t1' AND corpus_id='eng' AND document_id='d1' AND version='v1'"
    ).fetchone()
    assert row["lifecycle_revision"] == rev_before  # monotonic revision untouched
    assert row["security_level"] == "secret"
    assert row["acl_scope"] == "restricted"
    assert json.loads(row["allowed_user_ids"]) == ["u9"]
    store.close()
    os.unlink(path)
