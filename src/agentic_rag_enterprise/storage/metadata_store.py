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
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from agentic_rag_enterprise.domain.chunk import ChunkRecord
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
        self._conn = sqlite3.connect(db_path, isolation_level=None)
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
                ln
                for ln in script.splitlines()
                if ln.strip() and not ln.strip().startswith("--")
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
            lease = cur.execute(
                "SELECT owner_job_id, status, lease_generation FROM document_builds "
                "WHERE tenant_id=? AND corpus_id=? AND document_id=? AND document_version=?",
                (tenant_id, corpus_id, document_id, document_version),
            ).fetchone()
            if lease is None:
                generation = 1
                cur.execute(
                    "INSERT INTO document_builds "
                    "(tenant_id, corpus_id, document_id, document_version, "
                    " owner_job_id, status, base_revision, acquired_at, lease_generation) "
                    "VALUES (?, ?, ?, ?, ?, 'running', ?, ?, ?)",
                    (
                        tenant_id,
                        corpus_id,
                        document_id,
                        document_version,
                        job_id,
                        base_revision,
                        _now_iso(),
                        generation,
                    ),
                )
            else:
                owner = lease["owner_job_id"]
                generation = int(lease["lease_generation"]) + 1
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
                    # lease so a re-delivered job can rebuild idempotently.
                    cur.execute(
                        "UPDATE document_builds "
                        "SET owner_job_id=?, status='running', base_revision=?, "
                        "acquired_at=?, lease_generation=? "
                        "WHERE tenant_id=? AND corpus_id=? AND document_id=? "
                        "AND document_version=?",
                        (
                            job_id,
                            base_revision,
                            _now_iso(),
                            generation,
                            tenant_id,
                            corpus_id,
                            document_id,
                            document_version,
                        ),
                    )
                else:
                    # Same owner resume / retry: reset job + lease to running and
                    # advance the fencing token so a taken-over stale owner is
                    # rejected before mutating shared state (E-008.3 P1-2).
                    cur.execute(
                        "UPDATE document_builds "
                        "SET status='running', base_revision=?, acquired_at=?, "
                        "lease_generation=? "
                        "WHERE tenant_id=? AND corpus_id=? AND document_id=? "
                        "AND document_version=?",
                        (
                            base_revision,
                            _now_iso(),
                            generation,
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
                        parser_version, chunking_version, embedding_version, base_revision
                    ) VALUES (?, ?, ?, ?, ?, 'running', ?, ?, 0, 0, ?, ?, ?, ?)
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
                    # retrying owner is not seen as takeable (P1-2).
                    cur.execute(
                        "UPDATE ingestion_jobs SET status='running', finished_at=NULL, "
                        "error_code=NULL, error_message=NULL WHERE job_id=?",
                        (job_id,),
                    )
                    status = JobStatus.RUNNING

            # Capture the version this job replaces at acquire time (stable
            # across resume); the commit-phase CAS may return a different value
            # on resume, so it must not overwrite the persisted acquire value.
            prev_row = cur.execute(
                "SELECT previous_active_version FROM ingestion_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            if prev_row["previous_active_version"] is None:
                active = cur.execute(
                    "SELECT version FROM documents "
                    "WHERE tenant_id=? AND corpus_id=? AND document_id=? AND status='active'",
                    (tenant_id, corpus_id, document_id),
                ).fetchone()
                previous_version = active["version"] if active else None
                cur.execute(
                    "UPDATE ingestion_jobs SET previous_active_version = ? WHERE job_id = ?",
                    (previous_version, job_id),
                )

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
            cur.execute(
                f"INSERT INTO documents ({names}) VALUES ({placeholders})", cols
            )
        elif existing["status"] in ("processing", "failed"):
            assigns = ", ".join(
                f"{c} = :{c}" for c in cols if c != "lifecycle_revision"
            )
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
