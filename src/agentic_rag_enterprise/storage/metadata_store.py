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
            self._conn.executescript(path.read_text())
            self._conn.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (version, _now_iso()),
            )
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
    ) -> JobStatus:
        """Insert the job row if absent (CAS via PK). Returns the current status."""
        row = self._conn.execute(
            "SELECT status FROM ingestion_jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        if row is not None:
            return JobStatus(row["status"])
        self._conn.execute(
            """
            INSERT INTO ingestion_jobs (
                job_id, document_id, document_version, corpus_id, tenant_id,
                status, started_at, raw_hash, parent_count, child_count,
                parser_version, chunking_version, embedding_version
            ) VALUES (?, ?, ?, ?, ?, 'running', ?, ?, 0, 0, ?, ?, ?)
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
            ),
        )
        self.mark_step(job_id, "acquire", "done")
        return JobStatus.RUNNING

    def get_job_status(self, job_id: str) -> Optional[JobStatus]:
        row = self._conn.execute(
            "SELECT status FROM ingestion_jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        return JobStatus(row["status"]) if row else None

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
        self._conn.execute(
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

    def get_current_revision(self, tenant_id: str, corpus_id: str, document_id: str) -> int:
        row = self._conn.execute(
            "SELECT lifecycle_revision FROM documents "
            "WHERE tenant_id=? AND corpus_id=? AND document_id=? AND status='active'",
            (tenant_id, corpus_id, document_id),
        ).fetchone()
        return int(row["lifecycle_revision"]) if row else 0

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
    ) -> int:
        """Atomically switch the active version within one transaction.

        * increments ``lifecycle_revision`` monotonically,
        * deactivates the prior active version (status -> superseded),
        * activates ``new_version`` (status -> active, indexed_at set),
        * uses CAS on ``lifecycle_revision`` so a stale/competing job fails
          closed (build plan §10.10 #2, #4, #8).

        Returns the new ``lifecycle_revision``. Raises :class:`ActiveVersionConflict`
        if the race is lost or ``expected_revision`` is stale.
        """
        # ``BEGIN IMMEDIATE`` takes a RESERVED write lock up front, so only one
        # writer can be in this transaction at a time; sqlite does not support
        # ``SELECT ... FOR UPDATE`` (that syntax is postgres-only) and it is not
        # needed here. The monotonic ``lifecycle_revision`` CAS below is the
        # delete/update competition guard (build plan §10.10 #8).
        cur = self._conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        try:
            row = cur.execute(
                "SELECT version, lifecycle_revision FROM documents "
                "WHERE tenant_id=? AND corpus_id=? AND document_id=? AND status='active'",
                (tenant_id, corpus_id, document_id),
            ).fetchone()
            current_rev = int(row["lifecycle_revision"]) if row else 0
            if row is not None and current_rev != expected_revision:
                raise ActiveVersionConflict(
                    f"stale expected_revision={expected_revision}, current={current_rev}"
                )
            new_rev = current_rev + 1
            now = _now_iso()
            # The previously-active version is superseded: it MUST leave the
            # "active" state so retrieval (status=active & deprecated=false)
            # never serves a stale version. "deprecated" is the only
            # non-active lifecycle state in the schema/enum that also reads as
            # not-retrievable (§10.2 active->deprecated transition).
            if row is not None and row["version"] != new_version:
                cur.execute(
                    "UPDATE documents SET status='deprecated', deprecated=1, "
                    "effective_to=?, lifecycle_revision=? WHERE tenant_id=? AND "
                    "corpus_id=? AND document_id=? AND version=?",
                    (now, new_rev, tenant_id, corpus_id, document_id, row["version"]),
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
            return new_rev
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
