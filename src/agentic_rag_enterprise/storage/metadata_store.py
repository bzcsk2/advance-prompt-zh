"""Metadata DB: ingestion control-plane source of truth (build plan §10.10).

The Metadata DB is the **only** authority for document lifecycle and the active
version. Qdrant, the Parent Store and the filesystem are rebuildable data
planes and MUST NOT be used to infer final lifecycle state or which version is
active (§10.10 preamble).

Backed by stdlib ``sqlite3`` (no new dependency). Migrations under
``migrations/`` are applied once, tracked in ``schema_migrations``.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, TYPE_CHECKING

from agentic_rag_enterprise.domain.chunk import ChunkRecord

if TYPE_CHECKING:
    from agentic_rag_enterprise.storage.checkpoint_store import RunCheckpoint
from agentic_rag_enterprise.domain.corpus import CorpusConfig
from agentic_rag_enterprise.domain.document import SourceDocument
from agentic_rag_enterprise.domain.ingestion import (
    DocumentStatus,
    JobStatus,
)

MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "migrations"

_JSON_LIST_COLUMNS = (
    "allowed_user_ids",
    "allowed_group_ids",
    "denied_user_ids",
    "denied_group_ids",
)

_LIST_COLUMNS_DOC = _JSON_LIST_COLUMNS
# Chunks additionally serialize ``section_path`` (list) and ``metadata`` (dict).
_JSON_COLUMNS_CHUNK = _JSON_LIST_COLUMNS + ("section_path", "metadata")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _as_json_list(value) -> str:
    if value is None:
        return "[]"
    if isinstance(value, str):
        return value
    return json.dumps(list(value))


class _LockedSqliteConn:
    """Serialize access to a sqlite3 connection shared across threads.

    The dev/service container is built in the process main thread but HTTP
    requests are dispatched on worker threads (starlette ``TestClient`` / the
    ASGI server). ``check_same_thread=False`` lets the connection be used from
    those worker threads; the lock ensures statements from concurrent requests
    do not interleave on the single shared connection.
    """

    def __init__(self, conn: sqlite3.Connection, lock: threading.Lock) -> None:
        object.__setattr__(self, "_conn", conn)
        object.__setattr__(self, "_lock", lock)

    def execute(self, *args, **kwargs):
        with self._lock:
            return self._conn.execute(*args, **kwargs)

    def executemany(self, *args, **kwargs):
        with self._lock:
            return self._conn.executemany(*args, **kwargs)

    def commit(self):
        with self._lock:
            return self._conn.commit()

    def rollback(self):
        with self._lock:
            return self._conn.rollback()

    def cursor(self):
        return _LockedSqliteCursor(self._conn.cursor(), self._lock)

    def close(self):
        with self._lock:
            return self._conn.close()

    @property
    def row_factory(self):
        return self._conn.row_factory

    @row_factory.setter
    def row_factory(self, value):
        self._conn.row_factory = value

    def __getattr__(self, name):
        return getattr(self._conn, name)


class _LockedSqliteCursor:
    """Cursor that takes the shared connection lock per statement."""

    def __init__(self, cur: sqlite3.Cursor, lock: threading.Lock) -> None:
        object.__setattr__(self, "_cur", cur)
        object.__setattr__(self, "_lock", lock)

    def execute(self, *args, **kwargs):
        with self._lock:
            return self._cur.execute(*args, **kwargs)

    def executemany(self, *args, **kwargs):
        with self._lock:
            return self._cur.executemany(*args, **kwargs)

    def fetchone(self):
        with self._lock:
            return self._cur.fetchone()

    def fetchall(self):
        with self._lock:
            return self._cur.fetchall()

    def __iter__(self):
        with self._lock:
            return list(self._cur)

    def __getattr__(self, name):
        return getattr(self._cur, name)


class ActiveVersionConflict(Exception):
    """Raised when an active-version switch loses a race or uses a stale revision.

    Fail-closed: the competing job must not corrupt the currently visible
    (newer) active version (build plan §10.10 #2, #8).
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class JobIdentityConflict(Exception):
    """Raised when a ``job_id`` is reused with a different immutable request.

    A ``job_id`` is an immutable binding to (tenant, corpus, document, version,
    raw_hash). Reusing it for a different artifact must fail closed before any
    document row is mutated (build plan §10.10 #2, E-008.1 P1-6).
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class VersionContentConflict(Exception):
    """Raised when the same ``(document_id, version)`` is re-ingested with a
    different ``content_hash``.

    The existing version must not be overwritten (build plan §10.4, E-008.1 P1-2).
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class IndexSwitchConflict(Exception):
    """Raised when an index switch (pointer flip) cannot be taken because another
    in-flight switch already holds the per-corpus lease.

    Serializes ``switch_index`` / ``rollback_index`` so the persisted pointer and
    the live in-memory registry cannot be left inconsistent by concurrent
    switches (E-022 contract P1-3).
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class BuildConflict(Exception):
    """Raised when a build for a ``(tenant, corpus, document, version)`` is
    already owned by a different in-flight job, or when a build's lease has been
    taken over (fencing token advanced) by a concurrent delivery.

    The lease guarantees exactly one in-flight build per artifact at a time, so a
    concurrent delivery cannot race on the shared data plane (deterministic
    content-addressed IDs) and delete the winning build's artifacts. A stale
    owner whose lease generation has advanced is also rejected before it can
    mutate shared state (E-008.2 P1-3 / P1-4, E-008.3 P1-2 fencing).
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class MetadataStore:
    """SQLite-backed metadata store for documents, jobs, chunks and steps."""

    def __init__(self, db_path: str = "metadata.db") -> None:
        self._db_path = db_path
        # autocommit off; explicit BEGIN/COMMIT for multi-statement transactions.
        # The container is built in the main thread but HTTP requests run on
        # worker threads (TestClient / ASGI server), so check_same_thread=False
        # is required; the _LockedSqliteConn proxy serializes statements so
        # concurrent requests cannot interleave on the single shared connection.
        raw = sqlite3.connect(db_path, isolation_level=None, check_same_thread=False)
        self._lock = threading.Lock()
        self._conn = _LockedSqliteConn(raw, self._lock)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self.apply_migrations()

    # ------------------------------------------------------------------ #
    # Migrations
    # ------------------------------------------------------------------ #
    def apply_migrations(self) -> None:
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations "
            "(version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        applied = {
            r["version"] for r in self._conn.execute("SELECT version FROM schema_migrations")
        }
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            version = path.stem
            if version in applied:
                continue
            # Apply the DDL and the schema_migrations marker inside ONE explicit
            # transaction so a crash mid-migration cannot leave a column added but
            # unrecorded (which would otherwise fail on the next boot with a
            # duplicate-column error). With isolation_level=None the connection is
            # in autocommit, so we manage the transaction manually with
            # BEGIN IMMEDIATE / COMMIT / ROLLBACK and split the script into
            # individual statements (executescript would auto-COMMIT and break
            # atomicity).
            script = path.read_text().strip()
            marker = (
                f"INSERT INTO schema_migrations(version, applied_at) "
                f"VALUES ('{version}', '{_now_iso()}');"
            )
            # Drop full-line SQL comments (and blanks) so the per-statement
            # execute() never receives a comment-only statement, then split on
            # ";" into individual statements (our migration files keep ";" only
            # as a statement separator, never inside string literals).
            sql_lines = [
                ln for ln in script.splitlines() if ln.strip() and not ln.strip().startswith("--")
            ]
            statements = [
                s.strip() for s in ("\n".join(sql_lines) + "\n" + marker).split(";") if s.strip()
            ]
            cur = self._conn.cursor()
            try:
                cur.execute("BEGIN IMMEDIATE")
                for stmt in statements:
                    cur.execute(stmt)
                cur.execute("COMMIT")
            except Exception:
                cur.execute("ROLLBACK")
                raise
            applied.add(version)

    def close(self) -> None:
        self._conn.close()

    # ------------------------------------------------------------------ #
    # Jobs
    # ------------------------------------------------------------------ #
    def acquire_job(
        self,
        *,
        job_id: str,
        document_id: str,
        document_version: str,
        corpus_id: str,
        tenant_id: str,
        parser_version: str,
        chunking_version: str,
        embedding_version: str,
        raw_hash: str,
        base_revision: int,
        document: Optional[SourceDocument] = None,
        attempt_id: Optional[str] = None,
        recover: bool = False,
    ) -> tuple[JobStatus, int]:
        """Atomically claim/renew the build lease and the job row.

        Runs entirely inside one ``BEGIN IMMEDIATE`` transaction so the build
        lease, the processing document row, the job-row insert, the
        immutable-identity check, and the previous-active-version capture are a
        single atomic action (E-008.2 P1-3 / P1-4, E-008.3 P1-1 claim-before-
        mutate). This is the FIRST mutation a job makes, before any
        Parent/Qdrant/Chunk write, so a ``BuildConflict`` loser never touches
        shared data (and therefore never compensates the winner's artifacts):

        * No lease for ``(tenant, corpus, document, version)`` -> claim it
          (owner = this job, ``lease_generation = 1``).
        * Lease owned by THIS job -> resume / retry: reset the job and lease to
          ``running`` and advance ``lease_generation`` (fencing token) so a
          stale taken-over owner is rejected downstream (E-008.3 P1-2).
        * Lease owned by a DIFFERENT in-flight job (running/queued/cancelling)
          -> :class:`BuildConflict` (the concurrent build owns the shared data
          plane; do not race on deterministic IDs).
        * Lease owned by a terminal job (succeeded/failed/cancelled) -> takeover:
          reassign the lease to this job, advance ``lease_generation``, and
          rebuild (idempotent re-delivery).

        ``base_revision`` is the monotonic lifecycle revision captured at acquire
        time and persisted so the commit-phase CAS rejects this job if a newer
        revision lands first (build plan §10.10 #8, E-008.1 P1-3).

        Returns ``(job_status, lease_generation)``. Raises
        :class:`JobIdentityConflict` if ``job_id`` is already bound to a different
        immutable request, and :class:`BuildConflict` for a concurrent in-flight
        build.
        """
        cur = self._conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        try:
            if attempt_id is None:
                attempt_id = uuid.uuid4().hex
            lease = cur.execute(
                "SELECT owner_job_id, status, lease_generation, previous_active_version, "
                "attempt_id "
                "FROM document_builds "
                "WHERE tenant_id=? AND corpus_id=? AND document_id=? AND document_version=?",
                (tenant_id, corpus_id, document_id, document_version),
            ).fetchone()
            if lease is None:
                generation = 1
                # P1-2: capture the version this BUILD replaces at the FIRST lease
                # claim. It is bound to the build (lease) identity, not to a job,
                # so a replacement job taking over the lease later inherits it and
                # never recomputes it against the (already-switched) active version
                # (E-008.4 P1-2).
                active = cur.execute(
                    "SELECT version FROM documents "
                    "WHERE tenant_id=? AND corpus_id=? AND document_id=? AND status='active'",
                    (tenant_id, corpus_id, document_id),
                ).fetchone()
                previous_version = active["version"] if active else None
                cur.execute(
                    "INSERT INTO document_builds "
                    "(tenant_id, corpus_id, document_id, document_version, "
                    " owner_job_id, status, base_revision, acquired_at, lease_generation, "
                    " previous_active_version, attempt_id, claimed_at) "
                    "VALUES (?, ?, ?, ?, ?, 'running', ?, ?, ?, ?, ?, ?)",
                    (
                        tenant_id,
                        corpus_id,
                        document_id,
                        document_version,
                        job_id,
                        base_revision,
                        _now_iso(),
                        generation,
                        previous_version,
                        attempt_id,
                        _now_iso(),
                    ),
                )
            else:
                owner = lease["owner_job_id"]
                generation = int(lease["lease_generation"]) + 1
                # Carry the lease-bound previous_active_version forward; never
                # recompute it on takeover/resume (E-008.4 P1-2).
                previous_version = lease["previous_active_version"]
                if owner != job_id:
                    owner_status = self.get_job_status(owner)
                    if owner_status in (
                        JobStatus.RUNNING,
                        JobStatus.QUEUED,
                        JobStatus.CANCELLING,
                    ):
                        raise BuildConflict(
                            f"build for ({tenant_id},{corpus_id},{document_id},"
                            f"{document_version}) owned by in-flight job {owner!r}"
                        )
                    # Terminal owner (succeeded/failed/cancelled): take over the
                    # lease so a re-delivered job can rebuild idempotently. The
                    # lease-bound previous_active_version travels with the lease.
                    cur.execute(
                        "UPDATE document_builds "
                        "SET owner_job_id=?, status='running', base_revision=?, "
                        "acquired_at=?, lease_generation=?, previous_active_version=?, "
                        "attempt_id=?, claimed_at=? "
                        "WHERE tenant_id=? AND corpus_id=? AND document_id=? "
                        "AND document_version=?",
                        (
                            job_id,
                            base_revision,
                            _now_iso(),
                            generation,
                            previous_version,
                            attempt_id,
                            _now_iso(),
                            tenant_id,
                            corpus_id,
                            document_id,
                            document_version,
                        ),
                    )
                else:
                    # Same owner (job_id) re-acquiring. Two distinct execution
                    # attempts are now distinguishable at the DB level via
                    # attempt_id (E-008.4 P1-3, cross-process). A live RUNNING
                    # lease claimed by a DIFFERENT attempt is a duplicate delivery
                    # (e.g. a second process), NOT a recovery: reject it with
                    # BuildConflict and do NOT advance the fencing generation, so
                    # the in-flight attempt keeps its authority over the
                    # deterministic data plane. Explicit recovery (recover=True,
                    # e.g. resuming a crashed attempt) or a dead (terminal) lease
                    # is allowed to advance.
                    # The build-lease status column uses its OWN vocabulary
                    # ('running' / 'done' / 'failed'), independent of the
                    # JobStatus enum, so compare the raw string rather than
                    # coercing through JobStatus (which has no 'done' member).
                    lease_is_running = lease["status"] == "running"
                    lease_attempt = lease["attempt_id"]
                    if lease_is_running and lease_attempt != attempt_id and not recover:
                        raise BuildConflict(
                            f"job {job_id!r} already has a live execution attempt "
                            f"(attempt_id={lease_attempt!r}); refusing duplicate "
                            f"attempt {attempt_id!r} on RUNNING lease"
                        )
                    # Same owner resume / retry / recovery: reset job + lease to
                    # running and advance the fencing token so a taken-over stale
                    # owner is rejected before mutating shared state (E-008.3
                    # P1-2). The lease-bound previous_active_version is carried
                    # forward; the new attempt_id + claimed_at are persisted.
                    cur.execute(
                        "UPDATE document_builds "
                        "SET status='running', base_revision=?, acquired_at=?, "
                        "lease_generation=?, previous_active_version=?, "
                        "attempt_id=?, claimed_at=? "
                        "WHERE tenant_id=? AND corpus_id=? AND document_id=? "
                        "AND document_version=?",
                        (
                            base_revision,
                            _now_iso(),
                            generation,
                            previous_version,
                            attempt_id,
                            _now_iso(),
                            tenant_id,
                            corpus_id,
                            document_id,
                            document_version,
                        ),
                    )

            # Processing document row (optional): insert, or refresh metadata for
            # an existing uncommitted row. Never downgrade an already-active /
            # deprecated row (a resume after a commit-crash must not clobber the
            # committed lifecycle state managed by commit/publish).
            if document is not None:
                self._upsert_document_build(cur, document)

            # Job row: insert if absent, else verify the immutable identity
            # atomically (folded from validate_job_identity so there is no
            # race window before the insert). A terminal/failed job that is
            # re-acquired by the SAME owner resets to running (P1-2).
            row = cur.execute(
                "SELECT tenant_id, corpus_id, document_id, document_version, "
                "raw_hash, status FROM ingestion_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            if row is None:
                cur.execute(
                    """
                    INSERT INTO ingestion_jobs (
                        job_id, document_id, document_version, corpus_id, tenant_id,
                        status, started_at, raw_hash, parent_count, child_count,
                        parser_version, chunking_version, embedding_version, base_revision,
                        previous_active_version
                    ) VALUES (?, ?, ?, ?, ?, 'running', ?, ?, 0, 0, ?, ?, ?, ?, ?)
                    """,
                    (
                        job_id,
                        document_id,
                        document_version,
                        corpus_id,
                        tenant_id,
                        _now_iso(),
                        raw_hash,
                        parser_version,
                        chunking_version,
                        embedding_version,
                        base_revision,
                        previous_version,
                    ),
                )
                status: JobStatus = JobStatus.RUNNING
            else:
                mismatched = (
                    row["tenant_id"] != tenant_id
                    or row["corpus_id"] != corpus_id
                    or row["document_id"] != document_id
                    or row["document_version"] != document_version
                    or (row["raw_hash"] or "") != raw_hash
                )
                if mismatched:
                    raise JobIdentityConflict(
                        f"job_id={job_id!r} already bound to a different request "
                        f"(tenant={row['tenant_id']!r} corpus={row['corpus_id']!r} "
                        f"doc={row['document_id']!r} version={row['document_version']!r})"
                    )
                status = JobStatus(row["status"])
                if status != JobStatus.RUNNING:
                    # Resuming a terminal/failed job: reset to running so a
                    # retrying owner is not seen as takeable (P1-2). Carry the
                    # lease-bound previous_active_version forward (E-008.4 P1-2).
                    cur.execute(
                        "UPDATE ingestion_jobs SET status='running', finished_at=NULL, "
                        "error_code=NULL, error_message=NULL, previous_active_version=? "
                        "WHERE job_id=?",
                        (previous_version, job_id),
                    )
                    status = JobStatus.RUNNING

            cur.execute("COMMIT")
            # The "acquire" step marker lets a resumed run skip re-claiming the
            # lease (the lease row itself persists as the real ownership record).
            self.mark_step(job_id, "acquire", "done")
            return status, generation
        except Exception:
            cur.execute("ROLLBACK")
            raise

    def get_lease_generation(
        self, tenant_id: str, corpus_id: str, document_id: str, document_version: str
    ) -> int:
        """Return the current ``lease_generation`` for the build, or 0 if none.

        Used by the ingestion job as a fencing token: a stale owner (taken over
        by a concurrent delivery) holds a generation lower than the live lease,
        so its downstream mutations are rejected with :class:`BuildConflict`
        (E-008.3 P1-2).
        """
        row = self._conn.execute(
            "SELECT lease_generation FROM document_builds "
            "WHERE tenant_id=? AND corpus_id=? AND document_id=? AND document_version=?",
            (tenant_id, corpus_id, document_id, document_version),
        ).fetchone()
        return int(row["lease_generation"]) if row else 0

    def _upsert_document_build(self, cur, doc: SourceDocument) -> None:
        """Insert the processing document row, or refresh metadata for an
        existing uncommitted row. Never downgrade an already-active / deprecated
        row: a resume after a commit-crash must not clobber the committed
        lifecycle state (which only commit/publish may change).
        """
        cols = self._document_columns(doc)
        existing = cur.execute(
            "SELECT status FROM documents WHERE tenant_id=? AND corpus_id=? "
            "AND document_id=? AND version=?",
            (doc.tenant_id, doc.corpus_id, doc.document_id, doc.version),
        ).fetchone()
        if existing is None:
            names = ", ".join(cols)
            placeholders = ", ".join(f":{c}" for c in cols)
            cur.execute(f"INSERT INTO documents ({names}) VALUES ({placeholders})", cols)
        elif existing["status"] in ("processing", "failed"):
            assigns = ", ".join(f"{c} = :{c}" for c in cols if c != "lifecycle_revision")
            cur.execute(
                f"UPDATE documents SET {assigns} "
                "WHERE tenant_id=:tenant_id AND corpus_id=:corpus_id "
                "AND document_id=:document_id AND version=:version",
                cols,
            )
        # else: active / deprecated -> preserve (managed by commit/publish)

    def get_build_owner(
        self, tenant_id: str, corpus_id: str, document_id: str, document_version: str
    ) -> Optional[str]:
        """Return the ``job_id`` that currently holds the build lease, or None.

        Used by the ingestion job to decide whether an already-active version is
        fully published (owner terminal+successful) or still mid-build / crashed
        (must resume rather than short-circuit to ALREADY_INDEXED).
        """
        row = self._conn.execute(
            "SELECT owner_job_id FROM document_builds "
            "WHERE tenant_id=? AND corpus_id=? AND document_id=? AND document_version=?",
            (tenant_id, corpus_id, document_id, document_version),
        ).fetchone()
        return row["owner_job_id"] if row else None

    def validate_job_identity(
        self,
        *,
        job_id: str,
        tenant_id: str,
        corpus_id: str,
        document_id: str,
        document_version: str,
        raw_hash: str,
    ) -> None:
        """Fail closed if ``job_id`` is reused with a different immutable request.

        Kept as a fast pre-check; the authoritative atomic check is inside
        :meth:`acquire_job` (E-008.2 P1-4).
        """
        row = self._conn.execute(
            "SELECT tenant_id, corpus_id, document_id, document_version, raw_hash "
            "FROM ingestion_jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        if row is None:
            return
        mismatched = (
            row["tenant_id"] != tenant_id
            or row["corpus_id"] != corpus_id
            or row["document_id"] != document_id
            or row["document_version"] != document_version
            or (row["raw_hash"] or "") != raw_hash
        )
        if mismatched:
            raise JobIdentityConflict(
                f"job_id={job_id!r} already bound to a different request "
                f"(tenant={row['tenant_id']!r} corpus={row['corpus_id']!r} "
                f"doc={row['document_id']!r} version={row['document_version']!r})"
            )

    def get_job_base_revision(self, job_id: str) -> int:
        row = self._conn.execute(
            "SELECT base_revision FROM ingestion_jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        return int(row["base_revision"]) if row else 0

    def get_job_previous_version(self, job_id: str) -> Optional[str]:
        row = self._conn.execute(
            "SELECT previous_active_version FROM ingestion_jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        return row["previous_active_version"] if row else None

    def set_job_previous_version(self, job_id: str, version: Optional[str]) -> None:
        self._conn.execute(
            "UPDATE ingestion_jobs SET previous_active_version = ? WHERE job_id = ?",
            (version, job_id),
        )

    def set_job_manifest(self, job_id: str, manifest_json: str) -> None:
        self._conn.execute(
            "UPDATE ingestion_jobs SET manifest = ? WHERE job_id = ?",
            (manifest_json, job_id),
        )

    def get_job_status(self, job_id: str) -> Optional[JobStatus]:
        row = self._conn.execute(
            "SELECT status FROM ingestion_jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        return JobStatus(row["status"]) if row else None

    def get_job_identity(self, job_id: str) -> Optional[dict]:
        """Return the immutable identity bound to ``job_id``, or None if no row.

        Used by the ingestion job to fail closed on a reused ``job_id`` even on
        the idempotent ALREADY_INDEXED path (E-008.2 P1-4).
        """
        row = self._conn.execute(
            "SELECT tenant_id, corpus_id, document_id, document_version, raw_hash "
            "FROM ingestion_jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        return dict(row) if row else None

    def mark_job_terminal(
        self,
        job_id: str,
        status: JobStatus,
        *,
        parent_count: int = 0,
        child_count: int = 0,
        error_code: Optional[str] = None,
        error_message: Optional[str] = None,
    ) -> None:
        # Update the job row AND the build lease in one transaction so the lease
        # state can never lag the job state (E-008.3 P1-2): a failed job must
        # show 'failed' on its lease (and a succeeded job 'done') so a
        # re-delivered job is correctly diagnosed as terminal vs in-flight.
        cur = self._conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        try:
            cur.execute(
                """
                UPDATE ingestion_jobs
                   SET status = ?, finished_at = ?, parent_count = ?, child_count = ?,
                        error_code = ?, error_message = ?
                  WHERE job_id = ?
                """,
                (
                    status.value,
                    _now_iso(),
                    parent_count,
                    child_count,
                    error_code,
                    error_message,
                    job_id,
                ),
            )
            if status in (JobStatus.SUCCEEDED, JobStatus.FAILED):
                lease_status = "done" if status == JobStatus.SUCCEEDED else "failed"
                cur.execute(
                    "UPDATE document_builds SET status = ? WHERE owner_job_id = ?",
                    (lease_status, job_id),
                )
            cur.execute("COMMIT")
        except Exception:
            cur.execute("ROLLBACK")
            raise

    # ------------------------------------------------------------------ #
    # Step markers (build plan §10.10 #3: reentrant, idempotent)
    # ------------------------------------------------------------------ #
    def mark_step(self, job_id: str, step_name: str, status: str) -> None:
        self._conn.execute(
            """
            INSERT INTO job_steps (job_id, step_name, status, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(job_id, step_name)
            DO UPDATE SET status = excluded.status, updated_at = excluded.updated_at
            """,
            (job_id, step_name, status, _now_iso()),
        )

    def is_step_done(self, job_id: str, step_name: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM job_steps WHERE job_id = ? AND step_name = ? AND status = 'done'",
            (job_id, step_name),
        ).fetchone()
        return row is not None

    def list_done_steps(self, job_id: str) -> list[str]:
        return [
            r["step_name"]
            for r in self._conn.execute(
                "SELECT step_name FROM job_steps WHERE job_id = ? AND status = 'done' ORDER BY rowid",
                (job_id,),
            )
        ]

    def clear_steps(self, job_id: str) -> None:
        # Drop step markers so a compensated (failed) job re-runs the full
        # pipeline from scratch on resume, rather than skipping a step whose
        # data-plane artifact was deleted by compensation (build plan §10.10 #7).
        self._conn.execute("DELETE FROM job_steps WHERE job_id = ?", (job_id,))

    # ------------------------------------------------------------------ #
    # Documents
    # ------------------------------------------------------------------ #
    def upsert_document(self, doc: SourceDocument) -> None:
        """Insert a new version row, or refresh metadata on an existing one.

        The active-version switch is performed by :meth:`commit_active_version`,
        not here, so this never flips ``status`` to ``active`` for an existing row.
        """
        exists = self._conn.execute(
            "SELECT 1 FROM documents WHERE tenant_id=? AND corpus_id=? AND document_id=? AND version=?",
            (doc.tenant_id, doc.corpus_id, doc.document_id, doc.version),
        ).fetchone()
        cols = self._document_columns(doc)
        if exists:
            assigns = ", ".join(f"{c} = :{c}" for c in cols if c != "lifecycle_revision")
            self._conn.execute(
                f"UPDATE documents SET {assigns} "
                "WHERE tenant_id=:tenant_id AND corpus_id=:corpus_id "
                "AND document_id=:document_id AND version=:version",
                cols,
            )
        else:
            names = ", ".join(cols)
            placeholders = ", ".join(f":{c}" for c in cols)
            self._conn.execute(f"INSERT INTO documents ({names}) VALUES ({placeholders})", cols)

    def register_corpus(self, corpus: CorpusConfig) -> None:
        """Register (or refresh) a corpus in the ``corpus_registry`` control-plane table.

        The ``documents`` table has a foreign key to ``corpus_registry``, so a
        corpus MUST be registered before any document is ingested into it. Safe
        to call repeatedly (idempotent upsert keyed by ``(corpus_id, tenant_id)``).
        """
        self._conn.execute(
            """
            INSERT INTO corpus_registry (
                corpus_id, tenant_id, name, description, domain, owner, source_type,
                vector_collection, parent_store_namespace, enabled, searchable,
                authority_level, freshness_sla_hours, security_policy_id,
                default_security_level, capability_ids, metadata_schema,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(corpus_id, tenant_id) DO UPDATE SET
                name=excluded.name,
                description=excluded.description,
                domain=excluded.domain,
                owner=excluded.owner,
                source_type=excluded.source_type,
                vector_collection=excluded.vector_collection,
                parent_store_namespace=excluded.parent_store_namespace,
                enabled=excluded.enabled,
                searchable=excluded.searchable,
                authority_level=excluded.authority_level,
                freshness_sla_hours=excluded.freshness_sla_hours,
                security_policy_id=excluded.security_policy_id,
                default_security_level=excluded.default_security_level,
                capability_ids=excluded.capability_ids,
                metadata_schema=excluded.metadata_schema,
                updated_at=excluded.updated_at
            """,
            (
                corpus.corpus_id,
                corpus.tenant_id,
                corpus.name,
                corpus.description,
                corpus.domain,
                corpus.owner,
                corpus.source_type,
                corpus.vector_collection,
                corpus.parent_store_namespace,
                int(corpus.enabled),
                int(corpus.searchable),
                corpus.authority_level,
                corpus.freshness_sla_hours,
                corpus.security_policy_id,
                corpus.default_security_level,
                json.dumps(corpus.capability_ids),
                json.dumps(corpus.metadata_schema),
                corpus.created_at.isoformat(),
                corpus.updated_at.isoformat(),
            ),
        )

    def get_document(
        self, tenant_id: str, corpus_id: str, document_id: str, version: str
    ) -> Optional[SourceDocument]:
        row = self._conn.execute(
            "SELECT * FROM documents WHERE tenant_id=? AND corpus_id=? AND document_id=? AND version=?",
            (tenant_id, corpus_id, document_id, version),
        ).fetchone()
        return self._row_to_document(row) if row else None

    def get_active_document(
        self, tenant_id: str, corpus_id: str, document_id: str
    ) -> Optional[SourceDocument]:
        row = self._conn.execute(
            "SELECT * FROM documents WHERE tenant_id=? AND corpus_id=? AND document_id=? AND status='active'",
            (tenant_id, corpus_id, document_id),
        ).fetchone()
        return self._row_to_document(row) if row else None

    def get_active_version(self, tenant_id: str, corpus_id: str, document_id: str) -> Optional[str]:
        """Control-plane active version for a document (build plan §10.10 #5).

        Retrieval gates on this so a freshly-committed active version is the only
        one that may reach the model, regardless of data-plane cleanup lag.
        """
        row = self._conn.execute(
            "SELECT version FROM documents "
            "WHERE tenant_id=? AND corpus_id=? AND document_id=? AND status='active'",
            (tenant_id, corpus_id, document_id),
        ).fetchone()
        return row["version"] if row else None

    def get_current_revision(self, tenant_id: str, corpus_id: str, document_id: str) -> int:
        # Monotonic over ALL versions of the document (not just the active row),
        # so delete/update competition is still ordered when no active row exists
        # (build plan §10.10 #8, E-008.1 P1-3).
        row = self._conn.execute(
            "SELECT MAX(lifecycle_revision) AS m FROM documents "
            "WHERE tenant_id=? AND corpus_id=? AND document_id=?",
            (tenant_id, corpus_id, document_id),
        ).fetchone()
        return int(row["m"]) if row and row["m"] is not None else 0

    def get_superseded_versions(
        self, tenant_id: str, corpus_id: str, document_id: str, *, exclude_version: str
    ) -> list[str]:
        rows = self._conn.execute(
            "SELECT version FROM documents "
            "WHERE tenant_id=? AND corpus_id=? AND document_id=? "
            "AND version <> ? AND status <> 'active' AND status <> 'deleted'",
            (tenant_id, corpus_id, document_id, exclude_version),
        ).fetchall()
        return [r["version"] for r in rows]

    def commit_active_version(
        self,
        *,
        tenant_id: str,
        corpus_id: str,
        document_id: str,
        new_version: str,
        expected_revision: int,
    ) -> tuple[int, Optional[str]]:
        """Atomically switch the active version within one transaction.

        * increments ``lifecycle_revision`` monotonically (over ALL versions),
        * deactivates the prior active version (status -> deprecated),
        * activates ``new_version`` (status -> active, indexed_at set),
        * uses CAS on ``lifecycle_revision`` so a stale/competing job fails
          closed (build plan §10.10 #2, #4, #8).

        Returns ``(new_lifecycle_revision, previous_active_version)``. Raises
        :class:`ActiveVersionConflict` if the race is lost or ``expected_revision``
        is stale. ``previous_active_version`` is ``None`` when there was no prior
        active version; the caller persists it so ``publish`` only deprecates the
        version this transaction actually replaced (E-008.1 P1-7).
        """
        # ``BEGIN IMMEDIATE`` takes a RESERVED write lock up front, so only one
        # writer can be in this transaction at a time; sqlite does not support
        # ``SELECT ... FOR UPDATE`` (that syntax is postgres-only) and it is not
        # needed here. The monotonic ``lifecycle_revision`` CAS below is the
        # delete/update competition guard (build plan §10.10 #8).
        cur = self._conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        try:
            # Monotonic revision is taken over ALL versions, not just the active
            # row, so competition ordering survives a deleted/no-active state.
            rev_row = cur.execute(
                "SELECT MAX(lifecycle_revision) AS m FROM documents "
                "WHERE tenant_id=? AND corpus_id=? AND document_id=?",
                (tenant_id, corpus_id, document_id),
            ).fetchone()
            current_rev = int(rev_row["m"]) if rev_row and rev_row["m"] is not None else 0
            # Find the currently active version (if any) to replace.
            active_row = cur.execute(
                "SELECT version FROM documents "
                "WHERE tenant_id=? AND corpus_id=? AND document_id=? AND status='active'",
                (tenant_id, corpus_id, document_id),
            ).fetchone()
            previous_version = active_row["version"] if active_row else None
            # Idempotent resume: if new_version is already the active version
            # (e.g. a crash after the previous commit left it active but the
            # commit step marker unwritten), treat the switch as already done and
            # return WITHOUT the stale-revision CAS check, so a resumed job that
            # re-runs commit with its persisted (now-stale) base_revision does not
            # fail closed (E-008.2 P1-1).
            if previous_version == new_version:
                cur.execute("COMMIT")
                return current_rev, previous_version
            new_rev = current_rev + 1
            now = _now_iso()
            if current_rev != expected_revision:
                raise ActiveVersionConflict(
                    f"stale expected_revision={expected_revision}, current={current_rev}"
                )
            # The previously-active version MUST leave the "active" state so
            # retrieval (status=active & deprecated=false, plus the control-plane
            # active-version gate) never serves a stale version.
            if previous_version is not None and previous_version != new_version:
                cur.execute(
                    "UPDATE documents SET status='deprecated', deprecated=1, "
                    "effective_to=?, lifecycle_revision=? WHERE tenant_id=? AND "
                    "corpus_id=? AND document_id=? AND version=?",
                    (now, new_rev, tenant_id, corpus_id, document_id, previous_version),
                )
            cur.execute(
                "UPDATE documents SET status='active', deprecated=0, "
                "effective_from=?, indexed_at=?, lifecycle_revision=? "
                "WHERE tenant_id=? AND corpus_id=? AND document_id=? AND version=?",
                (now, now, new_rev, tenant_id, corpus_id, document_id, new_version),
            )
            if cur.rowcount == 0:
                raise ActiveVersionConflict(
                    f"new version {new_version} not found or already superseded"
                )
            cur.execute("COMMIT")
            return new_rev, previous_version
        except Exception:
            cur.execute("ROLLBACK")
            raise

    # ------------------------------------------------------------------ #
    # Chunks (data-plane bookkeeping; retrieval uses Qdrant, not this table)
    # ------------------------------------------------------------------ #
    def upsert_chunk_record(self, chunk: ChunkRecord) -> None:
        exists = self._conn.execute(
            "SELECT 1 FROM chunks WHERE chunk_id = ?", (chunk.chunk_id,)
        ).fetchone()
        cols = self._chunk_columns(chunk)
        if exists:
            assigns = ", ".join(f"{c} = :{c}" for c in cols if c != "chunk_id")
            self._conn.execute(f"UPDATE chunks SET {assigns} WHERE chunk_id = :chunk_id", cols)
        else:
            names = ", ".join(cols)
            placeholders = ", ".join(f":{c}" for c in cols)
            self._conn.execute(f"INSERT INTO chunks ({names}) VALUES ({placeholders})", cols)

    def list_chunk_records(
        self, tenant_id: str, corpus_id: str, document_id: str, document_version: str
    ) -> list[ChunkRecord]:
        rows = self._conn.execute(
            "SELECT * FROM chunks WHERE tenant_id=? AND corpus_id=? AND document_id=? "
            "AND document_version=?",
            (tenant_id, corpus_id, document_id, document_version),
        ).fetchall()
        return [self._row_to_chunk(r) for r in rows]

    def delete_chunk_records(
        self, tenant_id: str, corpus_id: str, document_id: str, document_version: str
    ) -> None:
        """Remove this version's control-plane chunk records (compensation)."""
        self._conn.execute(
            "DELETE FROM chunks WHERE tenant_id=? AND corpus_id=? AND document_id=? "
            "AND document_version=?",
            (tenant_id, corpus_id, document_id, document_version),
        )

    def mark_document_failed(
        self, tenant_id: str, corpus_id: str, document_id: str, document_version: str
    ) -> None:
        """On pre-commit failure, mark this version's row failed (not active).

        Only transitions a still-unactivated row (processing/failed); never
        touches an active row, so a failed concurrent job cannot deactivate the
        currently visible version (build plan §10.5, E-008.1 P1-5).
        """
        self._conn.execute(
            "UPDATE documents SET status='failed', deprecated=0 "
            "WHERE tenant_id=? AND corpus_id=? AND document_id=? AND version=? "
            "AND status IN ('processing', 'failed')",
            (tenant_id, corpus_id, document_id, document_version),
        )

    def set_document_status(
        self,
        tenant_id: str,
        corpus_id: str,
        document_id: str,
        version: str,
        status: DocumentStatus,
        deleted_at: Optional[datetime] = None,
    ) -> None:
        """Logical delete / lifecycle flip of a single (document, version) row.

        Idempotent: re-asserting the same status is a no-op. When ``status`` is
        ``DELETED`` the ``deleted_at`` timestamp is recorded (``SourceDocument``
        requires it). Other callers may pass ``deleted_at`` for non-deleted
        transitions too without effect.
        """
        self._conn.execute(
            "UPDATE documents SET status=?, deleted_at=? "
            "WHERE tenant_id=? AND corpus_id=? AND document_id=? AND version=?",
            (
                status.value,
                deleted_at.isoformat() if deleted_at else None,
                tenant_id,
                corpus_id,
                document_id,
                version,
            ),
        )

    def logical_delete(
        self,
        tenant_id: str,
        corpus_id: str,
        document_id: str,
        version: str,
        *,
        deleted_at: datetime,
    ) -> int:
        """Logical delete of a document: flip the ACTIVE version to ``DELETED`` and
        advance ``lifecycle_revision`` atomically, as the FIRST step of a
        control-plane-first delete (build plan §10.10 #8).

        This is the competition guard against an in-flight :class:`IngestionJob`
        acquired *before* the delete: such a job holds a ``base_revision`` older
        than the new revision, so its ``commit_active_version`` CAS fails closed
        and cannot resurrect a deleted document. Because the revision bump happens
        here — before any Qdrant/Parent-Store propagation — there is no window in
        which a stale update job can commit a new active version.

        The target is resolved *inside* the transaction: if ``version`` is no
        longer active (a concurrent update activated another), the currently
        active version is deleted instead, so a delete always removes the
        document's active visibility. Idempotent: re-asserting ``DELETED`` on an
        already-deleted document is a no-op and does not bump the revision again.
        """
        cur = self._conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        try:
            target = cur.execute(
                "SELECT version, lifecycle_revision, status FROM documents "
                "WHERE tenant_id=? AND corpus_id=? AND document_id=? AND version=?",
                (tenant_id, corpus_id, document_id, version),
            ).fetchone()
            if target is None:
                # Version does not exist (already purged) -> nothing to do.
                cur.execute("COMMIT")
                return self.get_current_revision(tenant_id, corpus_id, document_id)
            if target["status"] == "deleted":
                cur.execute("COMMIT")
                return int(target["lifecycle_revision"])
            if target["status"] != "active":
                # The passed version was superseded by a concurrent update; delete
                # the currently active version so active visibility is removed.
                active = cur.execute(
                    "SELECT version, lifecycle_revision FROM documents "
                    "WHERE tenant_id=? AND corpus_id=? AND document_id=? AND status='active' "
                    "ORDER BY lifecycle_revision DESC, rowid DESC LIMIT 1",
                    (tenant_id, corpus_id, document_id),
                ).fetchone()
                if active is None:
                    cur.execute("COMMIT")
                    return self.get_current_revision(tenant_id, corpus_id, document_id)
                target_version = active["version"]
                current = int(active["lifecycle_revision"])
            else:
                target_version = target["version"]
                current = int(target["lifecycle_revision"])
            new_rev = current + 1
            cur.execute(
                "UPDATE documents SET status='deleted', deprecated=1, deleted_at=?, "
                "lifecycle_revision=? "
                "WHERE tenant_id=? AND corpus_id=? AND document_id=? AND version=?",
                (
                    deleted_at.isoformat(),
                    new_rev,
                    tenant_id,
                    corpus_id,
                    document_id,
                    target_version,
                ),
            )
            cur.execute("COMMIT")
            return new_rev
        except Exception:
            cur.execute("ROLLBACK")
            raise

    def get_document_latest(
        self, tenant_id: str, corpus_id: str, document_id: str
    ) -> Optional[SourceDocument]:
        """Return the version row with the highest ``lifecycle_revision``.

        Unlike :meth:`get_active_document`, this ignores status, so a logically
        deleted document can still be resolved (for authorize-then-purge).
        """
        row = self._conn.execute(
            "SELECT * FROM documents WHERE tenant_id=? AND corpus_id=? AND document_id=? "
            "ORDER BY lifecycle_revision DESC, rowid DESC LIMIT 1",
            (tenant_id, corpus_id, document_id),
        ).fetchone()
        return self._row_to_document(row) if row else None

    def list_document_versions(self, tenant_id: str, corpus_id: str, document_id: str) -> list[str]:
        """All version rows for a document (active, deprecated, failed, deleted)."""
        rows = self._conn.execute(
            "SELECT version FROM documents WHERE tenant_id=? AND corpus_id=? AND document_id=?",
            (tenant_id, corpus_id, document_id),
        ).fetchall()
        return [r["version"] for r in rows]

    def update_document_acl(
        self,
        tenant_id: str,
        corpus_id: str,
        document_id: str,
        version: str,
        *,
        security_level: str,
        acl_scope: str,
        allowed_user_ids: list[str],
        allowed_group_ids: list[str],
        denied_user_ids: list[str],
        denied_group_ids: list[str],
    ) -> None:
        """Patch the ACL columns of one (document, version) row AND advance
        ``lifecycle_revision`` atomically, as the FIRST step of a control-plane-
        first ACL tighten (build plan §10.7 / §10.10 #8).

        The revision advance is the fencing guard: an :class:`IngestionJob`
        acquired *before* the ACL tighten captured an older ``base_revision``, so
        its ``commit_active_version`` CAS fails and it cannot publish a new
        version carrying the pre-tighten ACL. The active check inside the
        transaction means a tighten that races a concurrent logical delete is a
        no-op (the delete wins; retrieval filters the document regardless).
        No vectors change (payload-only, §10.7).
        """
        cur = self._conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        try:
            row = cur.execute(
                "SELECT lifecycle_revision, status FROM documents "
                "WHERE tenant_id=? AND corpus_id=? AND document_id=? AND version=?",
                (tenant_id, corpus_id, document_id, version),
            ).fetchone()
            if row is None or row["status"] != "active":
                # Not active (concurrently deleted): no active version to fence.
                cur.execute("COMMIT")
                return
            current = int(row["lifecycle_revision"])
            new_rev = current + 1
            cur.execute(
                "UPDATE documents SET security_level=?, acl_scope=?, "
                "allowed_user_ids=?, allowed_group_ids=?, denied_user_ids=?, denied_group_ids=?, "
                "lifecycle_revision=? "
                "WHERE tenant_id=? AND corpus_id=? AND document_id=? AND version=?",
                (
                    security_level,
                    acl_scope,
                    _as_json_list(allowed_user_ids),
                    _as_json_list(allowed_group_ids),
                    _as_json_list(denied_user_ids),
                    _as_json_list(denied_group_ids),
                    new_rev,
                    tenant_id,
                    corpus_id,
                    document_id,
                    version,
                ),
            )
            cur.execute("COMMIT")
        except Exception:
            cur.execute("ROLLBACK")
            raise

    def delete_document(self, tenant_id: str, corpus_id: str, document_id: str) -> None:
        """Physical purge: remove every version row for a document. Idempotent.

        Child build/job rows that reference ``documents`` are removed first so the
        foreign-key constraint is not violated.
        """
        self._conn.execute(
            "DELETE FROM document_builds WHERE tenant_id=? AND corpus_id=? AND document_id=?",
            (tenant_id, corpus_id, document_id),
        )
        self._conn.execute(
            "DELETE FROM job_steps WHERE job_id IN ("
            "SELECT job_id FROM ingestion_jobs "
            "WHERE tenant_id=? AND corpus_id=? AND document_id=?)",
            (tenant_id, corpus_id, document_id),
        )
        self._conn.execute(
            "DELETE FROM ingestion_jobs WHERE tenant_id=? AND corpus_id=? AND document_id=?",
            (tenant_id, corpus_id, document_id),
        )
        self._conn.execute(
            "DELETE FROM documents WHERE tenant_id=? AND corpus_id=? AND document_id=?",
            (tenant_id, corpus_id, document_id),
        )

    # ------------------------------------------------------------------ #
    # E-022: reconciler + index migration + rollback
    # ------------------------------------------------------------------ #
    def acquire_reconciler_lease(self, corpus_id: str, owner: str, ttl_seconds: int = 300) -> bool:
        """Fence reconciler runs to a single active owner per corpus.

        Returns ``True`` if this ``owner`` now holds the lease (no live lease, an
        expired lease, or it already owns it). Returns ``False`` if another owner
        holds a live lease. Enables safe concurrent / repeated reconciliation
        (build plan §10.10 + E-022 contract invariants #5).
        """
        now = datetime.now(timezone.utc)
        expires = now + timedelta(seconds=ttl_seconds)
        row = self._conn.execute(
            "SELECT owner, expires_at FROM reconciler_leases WHERE corpus_id=?",
            (corpus_id,),
        ).fetchone()
        if row is None:
            self._conn.execute(
                "INSERT INTO reconciler_leases(corpus_id, owner, acquired_at, expires_at) "
                "VALUES (?, ?, ?, ?)",
                (corpus_id, owner, now.isoformat(), expires.isoformat()),
            )
            return True
        lease_expires = datetime.fromisoformat(row["expires_at"])
        if lease_expires <= now or row["owner"] == owner:
            self._conn.execute(
                "UPDATE reconciler_leases SET owner=?, acquired_at=?, expires_at=? "
                "WHERE corpus_id=?",
                (owner, now.isoformat(), expires.isoformat(), corpus_id),
            )
            return True
        return False

    def release_reconciler_lease(self, corpus_id: str, owner: str) -> None:
        self._conn.execute(
            "DELETE FROM reconciler_leases WHERE corpus_id=? AND owner=?",
            (corpus_id, owner),
        )

    def begin_reconciliation_run(
        self, run_id: str, corpus_id: str, started_at: str, *, dry_run: bool = False
    ) -> None:
        self._conn.execute(
            "INSERT INTO reconciliation_runs(run_id, corpus_id, started_at, dry_run) "
            "VALUES (?, ?, ?, ?)",
            (run_id, corpus_id, started_at, int(dry_run)),
        )

    def finish_reconciliation_run(
        self, run_id: str, finished_at: str, finding_count: int, *, mutated: bool = False
    ) -> None:
        self._conn.execute(
            "UPDATE reconciliation_runs SET finished_at=?, finding_count=?, mutated=? "
            "WHERE run_id=?",
            (finished_at, finding_count, int(mutated), run_id),
        )

    def record_control_plane_finding(
        self,
        *,
        corpus_id: str,
        kind: str,
        tenant_id: Optional[str] = None,
        document_id: Optional[str] = None,
        document_version: Optional[str] = None,
        detail: str = "",
    ) -> None:
        """Persist a control-plane finding as its own reconciliation run.

        Used when an automated control-plane operation (e.g. active-version
        rollback) cannot fully sync the data plane and needs the periodic
        :class:`Reconciler` to retry (build plan §10.10 / E-022 contract P1-2).
        The finding is durable and audit-visible even though no corpus-wide
        reconcile run produced it.
        """
        run_id = uuid.uuid4().hex
        self.begin_reconciliation_run(run_id, corpus_id, _now_iso(), dry_run=False)
        self.record_reconciliation_finding(
            run_id,
            kind=kind,
            corpus_id=corpus_id,
            tenant_id=tenant_id,
            document_id=document_id,
            document_version=document_version,
            detail=detail,
        )
        self.finish_reconciliation_run(run_id, _now_iso(), 1, mutated=False)

    def record_reconciliation_finding(
        self,
        run_id: str,
        *,
        kind: str,
        corpus_id: str,
        tenant_id: Optional[str] = None,
        document_id: Optional[str] = None,
        document_version: Optional[str] = None,
        detail: str = "",
    ) -> None:
        self._conn.execute(
            "INSERT INTO reconciliation_findings(run_id, kind, corpus_id, tenant_id, "
            "document_id, document_version, detail) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (run_id, kind, corpus_id, tenant_id, document_id, document_version, detail),
        )

    def iter_active_document_versions(self, corpus_id: str) -> list[tuple[str, str, str]]:
        """Return ``(tenant_id, document_id, version)`` for every ``active`` row.

        This is the reconciler's truth set: the data planes (Qdrant / Parent
        Store) are expected to contain exactly these artifacts. The tenant is
        included so the reconciler can scope data-plane lookups without a
        separate corpus-wide tenant lookup.
        """
        rows = self._conn.execute(
            "SELECT tenant_id, document_id, version FROM documents "
            "WHERE corpus_id=? AND status='active'",
            (corpus_id,),
        ).fetchall()
        return [(r["tenant_id"], r["document_id"], r["version"]) for r in rows]

    def iter_known_document_versions(self, corpus_id: str) -> list[tuple[str, str, str]]:
        """Return every ``(tenant_id, document_id, version)`` row (any status).

        The reconciler treats a Qdrant point / parent as a genuine orphan only
        when its ``(tenant_id, document_id, document_version)`` is absent from
        this set — a row that exists (even ``deleted``) is a known lifecycle
        state, handled by the purge path rather than force-deletion.
        """
        rows = self._conn.execute(
            "SELECT tenant_id, document_id, version FROM documents WHERE corpus_id=?",
            (corpus_id,),
        ).fetchall()
        return [(r["tenant_id"], r["document_id"], r["version"]) for r in rows]

    def iter_deleted_document_ids(self, corpus_id: str) -> list[tuple[str, str, str]]:
        """Return ``(tenant_id, document_id, version)`` rows in ``deleted`` status.

        The reconciler drives physical purge for these when their data plane
        still lingers (post-commit cleanup retry, build plan §10.10 #6).
        """
        rows = self._conn.execute(
            "SELECT tenant_id, document_id, version FROM documents "
            "WHERE corpus_id=? AND status='deleted'",
            (corpus_id,),
        ).fetchall()
        return [(r["tenant_id"], r["document_id"], r["version"]) for r in rows]

    def get_corpus_tenant(self, corpus_id: str) -> Optional[str]:
        """Tenant that owns a corpus (corpora are tenant-scoped)."""
        row = self._conn.execute(
            "SELECT tenant_id FROM documents WHERE corpus_id=? LIMIT 1",
            (corpus_id,),
        ).fetchone()
        if row:
            return row["tenant_id"]
        reg = self._conn.execute(
            "SELECT tenant_id FROM corpus_registry WHERE corpus_id=? LIMIT 1",
            (corpus_id,),
        ).fetchone()
        return reg["tenant_id"] if reg else None

    def iter_failed_job_ids(self, corpus_id: str) -> list[str]:
        """Return ``failed`` ingestion-job ids for a corpus (dead-letter)."""
        rows = self._conn.execute(
            "SELECT job_id FROM ingestion_jobs WHERE corpus_id=? AND status='failed'",
            (corpus_id,),
        ).fetchall()
        return [r["job_id"] for r in rows]

    def iter_child_chunks(self, corpus_id: str) -> list[dict]:
        """Return child-chunk control-plane records for re-embedding into a new index.

        Used by index migration to rebuild a parallel ``v2`` collection from the
        existing chunk content + ACL (no re-parsing). Payload fields mirror the
        Qdrant point schema written by :func:`child_chunk_to_point`.
        """
        rows = self._conn.execute(
            "SELECT chunk_id, parent_id, tenant_id, corpus_id, document_id, "
            "document_version, section_path, content, security_level, acl_scope, "
            "allowed_user_ids, allowed_group_ids, denied_user_ids, denied_group_ids "
            "FROM chunks WHERE corpus_id=? AND chunk_type='child'",
            (corpus_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def iter_active_child_chunks(self, corpus_id: str) -> list[dict]:
        """Return child-chunk records for the **active, non-deprecated** versions.

        Index migration builds a parallel collection from these only: a chunk is
        included iff its ``(tenant_id, corpus_id, document_id, document_version)``
        is an ``active`` and non-``deprecated`` row in ``documents``. This is what
        prevents the migrated index from resurrecting a deprecated or logically
        ``deleted`` version's evidence (build plan §10.8 + E-022 contract P1-1).
        """
        rows = self._conn.execute(
            "SELECT c.chunk_id, c.parent_id, c.tenant_id, c.corpus_id, c.document_id, "
            "c.document_version, c.section_path, c.content, c.security_level, c.acl_scope, "
            "c.allowed_user_ids, c.allowed_group_ids, c.denied_user_ids, c.denied_group_ids "
            "FROM chunks c "
            "JOIN documents d ON d.tenant_id=c.tenant_id AND d.corpus_id=c.corpus_id "
            "   AND d.document_id=c.document_id AND d.version=c.document_version "
            "WHERE c.corpus_id=? AND c.chunk_type='child' "
            "AND d.status='active' AND d.deprecated=0",
            (corpus_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_active_collection(self, corpus_id: str) -> Optional[str]:
        """Persisted active collection name (None => falls back to ``corpus_id``)."""
        row = self._conn.execute(
            "SELECT vector_collection FROM corpus_registry WHERE corpus_id=?",
            (corpus_id,),
        ).fetchone()
        return row["vector_collection"] if row else None

    def set_active_collection(self, corpus_id: str, collection_name: str) -> None:
        """Persist the active collection pointer (build plan §10.8 switch)."""
        self._conn.execute(
            "UPDATE corpus_registry SET vector_collection=? WHERE corpus_id=?",
            (collection_name, corpus_id),
        )

    # ------------------------------------------------------------------ #
    # Index-switch lease + atomic pointer flip (E-022 contract P1-3)
    # ------------------------------------------------------------------ #
    def acquire_index_switch_lease(
        self, corpus_id: str, owner: str, *, ttl_seconds: int = 300
    ) -> bool:
        """Claim the per-corpus index-switch lease atomically.

        Returns ``True`` if ``owner`` now holds the lease (newly acquired,
        re-acquired, or took over an expired lease), ``False`` if another owner
        holds a live lease. Serializes ``switch_index`` / ``rollback_index``.
        """
        cur = self._conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        try:
            row = cur.execute(
                "SELECT owner, acquired_at FROM index_switch_leases WHERE corpus_id=?",
                (corpus_id,),
            ).fetchone()
            now = _now_iso()
            if row is None:
                cur.execute(
                    "INSERT INTO index_switch_leases(corpus_id, owner, acquired_at) "
                    "VALUES (?, ?, ?)",
                    (corpus_id, owner, now),
                )
                cur.execute("COMMIT")
                return True
            if row["owner"] == owner:
                # Refresh the held lease (idempotent re-acquire).
                cur.execute(
                    "UPDATE index_switch_leases SET acquired_at=? WHERE corpus_id=?",
                    (now, corpus_id),
                )
                cur.execute("COMMIT")
                return True
            acquired = datetime.fromisoformat(row["acquired_at"])
            expired = (datetime.now(timezone.utc) - acquired).total_seconds() > ttl_seconds
            if expired:
                cur.execute("DELETE FROM index_switch_leases WHERE corpus_id=?", (corpus_id,))
                cur.execute(
                    "INSERT INTO index_switch_leases(corpus_id, owner, acquired_at) "
                    "VALUES (?, ?, ?)",
                    (corpus_id, owner, now),
                )
                cur.execute("COMMIT")
                return True
            cur.execute("COMMIT")
            return False
        except Exception:
            cur.execute("ROLLBACK")
            raise

    def release_index_switch_lease(self, corpus_id: str, owner: str) -> None:
        """Release the lease only if ``owner`` currently holds it (fencing)."""
        self._conn.execute(
            "DELETE FROM index_switch_leases WHERE corpus_id=? AND owner=?",
            (corpus_id, owner),
        )

    def set_active_collection_atomic(
        self, corpus_id: str, collection_name: Optional[str], owner: str
    ) -> None:
        """Flip the persisted pointer + mark the live build switched, in one txn.

        Must be called while ``owner`` holds the index-switch lease. Updates both
        ``corpus_registry.vector_collection`` (the durable retrieval pointer; a
        ``None`` value resets it to NULL, i.e. falls back to ``corpus_id``) and
        the matching ``index_builds.status='switched'`` so the persisted state and
        the index-build ledger agree (E-022 contract P1-3).
        """
        cur = self._conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        try:
            lease = cur.execute(
                "SELECT owner FROM index_switch_leases WHERE corpus_id=?",
                (corpus_id,),
            ).fetchone()
            if lease is None or lease["owner"] != owner:
                raise IndexSwitchConflict(
                    f"index switch lease for {corpus_id!r} not held by {owner!r}"
                )
            cur.execute(
                "UPDATE corpus_registry SET vector_collection=? WHERE corpus_id=?",
                (collection_name, corpus_id),
            )
            cur.execute(
                "UPDATE index_builds SET status='switched', finished_at=? "
                "WHERE corpus_id=? AND collection_name=?",
                (_now_iso(), corpus_id, collection_name),
            )
            cur.execute("COMMIT")
        except Exception:
            cur.execute("ROLLBACK")
            raise

    def begin_index_build(
        self,
        build_id: str,
        corpus_id: str,
        collection_name: str,
        embedding_version: str,
        chunking_version: str,
        previous_collection: Optional[str],
    ) -> None:
        self._conn.execute(
            "INSERT INTO index_builds(build_id, corpus_id, collection_name, "
            "embedding_version, chunking_version, status, previous_collection, started_at) "
            "VALUES (?, ?, ?, ?, ?, 'building', ?, ?)",
            (
                build_id,
                corpus_id,
                collection_name,
                embedding_version,
                chunking_version,
                previous_collection,
                _now_iso(),
            ),
        )

    def complete_index_build(self, build_id: str) -> None:
        self._conn.execute(
            "UPDATE index_builds SET status='active', finished_at=? WHERE build_id=?",
            (_now_iso(), build_id),
        )

    def get_index_build(self, build_id: str) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT * FROM index_builds WHERE build_id=?", (build_id,)
        ).fetchone()
        return dict(row) if row else None

    def rollback_active_version(
        self,
        *,
        tenant_id: str,
        corpus_id: str,
        document_id: str,
        to_version: Optional[str] = None,
        expected_revision: Optional[int] = None,
    ) -> tuple[int, Optional[str]]:
        """Temporarily roll a document's active version back (build plan §2630).

        The target version must still exist and must NOT be ``deleted`` (a purged
        version can never be reactivated). The switch is CAS-guarded on the
        monotonic ``lifecycle_revision`` (§10.10 #8) so a stale attempt cannot
        overwrite a newer committed revision. Returns ``(new_revision,
        previous_active_version)``. Raises :class:`ActiveVersionConflict` if there
        is no active version to roll back, no eligible candidate, or the CAS loses.
        """
        cur = self._conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        try:
            rev_row = cur.execute(
                "SELECT MAX(lifecycle_revision) AS m FROM documents "
                "WHERE tenant_id=? AND corpus_id=? AND document_id=?",
                (tenant_id, corpus_id, document_id),
            ).fetchone()
            current_rev = int(rev_row["m"]) if rev_row and rev_row["m"] is not None else 0
            active_row = cur.execute(
                "SELECT version FROM documents "
                "WHERE tenant_id=? AND corpus_id=? AND document_id=? AND status='active'",
                (tenant_id, corpus_id, document_id),
            ).fetchone()
            if active_row is None:
                raise ActiveVersionConflict(
                    f"no active version to roll back for document {document_id!r}"
                )
            previous_version = active_row["version"]
            # Resolve the rollback target.
            if to_version is not None:
                target = to_version
            else:
                cand = cur.execute(
                    "SELECT version FROM documents "
                    "WHERE tenant_id=? AND corpus_id=? AND document_id=? "
                    "AND status <> 'active' AND status <> 'deleted' "
                    "ORDER BY lifecycle_revision DESC LIMIT 1",
                    (tenant_id, corpus_id, document_id),
                ).fetchone()
                if cand is None:
                    raise ActiveVersionConflict(
                        f"no eligible rollback candidate for document {document_id!r}"
                    )
                target = cand["version"]
            target_row = cur.execute(
                "SELECT status FROM documents "
                "WHERE tenant_id=? AND corpus_id=? AND document_id=? AND version=?",
                (tenant_id, corpus_id, document_id, target),
            ).fetchone()
            if target_row is None:
                raise ActiveVersionConflict(f"rollback target version {target!r} not found")
            if target_row["status"] == "deleted":
                raise ActiveVersionConflict(f"refuse to reactivate purged version {target!r}")
            # Idempotent: target already active.
            if target == previous_version:
                cur.execute("COMMIT")
                return current_rev, previous_version
            if expected_revision is not None and current_rev != expected_revision:
                raise ActiveVersionConflict(
                    f"stale expected_revision={expected_revision}, current={current_rev}"
                )
            new_rev = current_rev + 1
            now = _now_iso()
            cur.execute(
                "UPDATE documents SET status='deprecated', deprecated=1, "
                "effective_to=?, lifecycle_revision=? WHERE tenant_id=? AND "
                "corpus_id=? AND document_id=? AND version=?",
                (now, new_rev, tenant_id, corpus_id, document_id, previous_version),
            )
            cur.execute(
                "UPDATE documents SET status='active', deprecated=0, "
                "effective_from=?, indexed_at=?, lifecycle_revision=? "
                "WHERE tenant_id=? AND corpus_id=? AND document_id=? AND version=?",
                (now, now, new_rev, tenant_id, corpus_id, document_id, target),
            )
            if cur.rowcount == 0:
                raise ActiveVersionConflict(f"rollback target {target!r} vanished during switch")
            cur.execute("COMMIT")
            return new_rev, previous_version
        except Exception:
            cur.execute("ROLLBACK")
            raise

    # ------------------------------------------------------------------ #
    # Run checkpoints (E-023 contract: persistent iteration-loop recovery)
    # ------------------------------------------------------------------ #
    def save_run_checkpoint(self, ck: "RunCheckpoint") -> None:  # noqa: F821 - imported below
        """Persist (insert or update) a run checkpoint with an immutable binding.

        The ``run_id`` is client-supplied, but the checkpoint is bound to an
        immutable identity — ``(tenant_id, user_id, session_id, query, corpus_id,
        policy_version)``. Within one transaction:

        * the ``run_id`` does not yet exist → ``INSERT`` a fresh ``running`` row;
        * it exists with the SAME identity → ``UPDATE`` only the resumable state
          (``round_index`` + ``state_json``), preserving the existing ``status``;
        * it exists with a DIFFERENT identity → raise
          :class:`CheckpointIdentityConflict` and leave the original row untouched
          (fail closed — a second principal cannot hijack / overwrite another's
          checkpoint, build plan §5.4).

        The identity columns are never overwritten, so an existing checkpoint
        cannot be re-bound to a different principal, query, or corpus.
        """
        from agentic_rag_enterprise.storage.checkpoint_store import (
            CHECKPOINT_RUNNING,
            CheckpointIdentityConflict,
            RunCheckpoint,  # noqa: F401 - used by the "RunCheckpoint" annotation (resolved by mypy)
        )

        now = _now_iso()
        identity = (
            ck.tenant_id,
            ck.user_id,
            ck.session_id,
            ck.query,
            ck.corpus_id,
            ck.policy_version,
        )
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            row = self._conn.execute(
                "SELECT tenant_id, user_id, session_id, query, corpus_id, "
                "policy_version FROM run_checkpoints WHERE run_id=?",
                (ck.run_id,),
            ).fetchone()
            if row is not None:
                existing = (
                    row["tenant_id"],
                    row["user_id"],
                    row["session_id"],
                    row["query"],
                    row["corpus_id"],
                    row["policy_version"],
                )
                if existing != identity:
                    raise CheckpointIdentityConflict(ck.run_id)
                # Same principal: only the resumable state moves; identity and
                # status columns are frozen (status is owned by the
                # complete/abort methods).
                self._conn.execute(
                    "UPDATE run_checkpoints SET round_index=?, state_json=?, "
                    "updated_at=? WHERE run_id=?",
                    (ck.round_index, ck.to_json(), now, ck.run_id),
                )
            else:
                self._conn.execute(
                    """
                    INSERT INTO run_checkpoints (
                        run_id, tenant_id, user_id, session_id, corpus_id, query,
                        policy_version, status, round_index, state_json,
                        created_at, updated_at, expires_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        ck.run_id,
                        ck.tenant_id,
                        ck.user_id,
                        ck.session_id,
                        ck.corpus_id,
                        ck.query,
                        ck.policy_version,
                        CHECKPOINT_RUNNING,  # status is owned by the save/complete/done methods
                        ck.round_index,
                        ck.to_json(),
                        now,
                        now,
                        None,
                    ),
                )
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    def load_run_checkpoint(self, run_id: str) -> "RunCheckpoint | None":  # noqa: F821
        from agentic_rag_enterprise.storage.checkpoint_store import RunCheckpoint

        row = self._conn.execute(
            "SELECT * FROM run_checkpoints WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            return None
        ck = RunCheckpoint.from_json(row["state_json"])
        # The denormalized columns must agree with the serialized state (defense
        # against a partial write); if they don't, treat the checkpoint as broken.
        if (
            ck.run_id != row["run_id"]
            or ck.tenant_id != row["tenant_id"]
            or ck.user_id != row["user_id"]
            or ck.session_id != row["session_id"]
            or ck.corpus_id != row["corpus_id"]
            or ck.policy_version != row["policy_version"]
        ):
            return None
        # The serialized ``status`` is a snapshot taken at save time and goes
        # stale once ``mark_run_checkpoint_done`` flips the DB column. Always
        # report the authoritative, current ``status`` from the row so
        # ``resume_run`` can refuse ``aborted`` and idempotently return
        # ``completed`` checkpoints (E-023 P1-3).
        return ck.model_copy(update={"status": row["status"]})

    def mark_run_checkpoint_done(self, run_id: str, *, aborted: bool = False) -> None:
        from agentic_rag_enterprise.storage.checkpoint_store import (
            CHECKPOINT_ABORTED,
            CHECKPOINT_COMPLETED,
        )

        status = CHECKPOINT_ABORTED if aborted else CHECKPOINT_COMPLETED
        self._conn.execute(
            "UPDATE run_checkpoints SET status=?, updated_at=? WHERE run_id=?",
            (status, _now_iso(), run_id),
        )

    def mark_run_checkpoint_running(self, run_id: str) -> None:
        """Flip a completed/aborted checkpoint back to 'running'.

        Used when a completed checkpoint must be re-entered because its evidence
        was revoked since the original completion (E-023 P1-3 residual: completed
        → running flip before recompute).
        """
        from agentic_rag_enterprise.storage.checkpoint_store import CHECKPOINT_RUNNING

        self._conn.execute(
            "UPDATE run_checkpoints SET status=?, updated_at=? WHERE run_id=?",
            (CHECKPOINT_RUNNING, _now_iso(), run_id),
        )

    def list_run_checkpoints(
        self, *, tenant_id: str, user_id: str, session_id: str
    ) -> list["RunCheckpoint"]:  # noqa: F821
        from agentic_rag_enterprise.storage.checkpoint_store import RunCheckpoint

        rows = self._conn.execute(
            "SELECT * FROM run_checkpoints WHERE tenant_id=? AND user_id=? AND session_id=?",
            (tenant_id, user_id, session_id),
        ).fetchall()
        out: list[RunCheckpoint] = []
        for row in rows:
            try:
                out.append(RunCheckpoint.from_json(row["state_json"]))
            except Exception:  # noqa: BLE001 - skip corrupt rows
                continue
        return out

    # ------------------------------------------------------------------ #
    # Row <-> model mapping
    # ------------------------------------------------------------------ #
    def _document_columns(self, doc: SourceDocument) -> dict:
        d = doc.model_dump()
        out: dict = {}
        for key, value in d.items():
            if key in _LIST_COLUMNS_DOC and isinstance(value, (list, tuple, set)):
                out[key] = _as_json_list(value)
            elif isinstance(value, datetime):
                out[key] = value.isoformat()
            elif isinstance(value, DocumentStatus):
                out[key] = value.value
            elif value is None:
                out[key] = None
            else:
                out[key] = value
        # lifecycle_revision is managed by commit_active_version; default to 0 here.
        out.setdefault("lifecycle_revision", 0)
        return out

    def _row_to_document(self, row: sqlite3.Row) -> SourceDocument:
        data = dict(row)
        for col in _LIST_COLUMNS_DOC:
            data[col] = json.loads(data[col]) if data.get(col) else []
        for date_col in (
            "effective_from",
            "effective_to",
            "indexed_at",
            "deleted_at",
            "discovered_at",
            "last_synced_at",
        ):
            if data.get(date_col):
                data[date_col] = datetime.fromisoformat(data[date_col])
        data["status"] = DocumentStatus(data["status"])
        return SourceDocument(**data)

    def _chunk_columns(self, chunk: ChunkRecord) -> dict:
        d = chunk.model_dump()
        out: dict = {}
        for key, value in d.items():
            if key in _JSON_LIST_COLUMNS and isinstance(value, (list, tuple, set)):
                out[key] = _as_json_list(value)
            elif key == "section_path":
                out[key] = _as_json_list(value)
            elif isinstance(value, dict):
                out[key] = json.dumps(value)
            elif isinstance(value, datetime):
                out[key] = value.isoformat()
            elif value is None:
                out[key] = None
            else:
                out[key] = value
        return out

    def _row_to_chunk(self, row: sqlite3.Row) -> ChunkRecord:
        data = dict(row)
        for col in _JSON_LIST_COLUMNS:
            data[col] = json.loads(data[col]) if data.get(col) else []
        if data.get("section_path"):
            data["section_path"] = json.loads(data["section_path"])
        else:
            data["section_path"] = []
        if data.get("metadata"):
            data["metadata"] = json.loads(data["metadata"])
        else:
            data["metadata"] = {}
        for date_col in ("effective_from", "effective_to"):
            if data.get(date_col):
                data[date_col] = datetime.fromisoformat(data[date_col])
        return ChunkRecord(**data)
