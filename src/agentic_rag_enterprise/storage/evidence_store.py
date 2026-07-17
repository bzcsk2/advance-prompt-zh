"""Evidence snapshot store (build plan §12.8).

Persists the immutable answer-time :class:`Evidence` snapshots produced by the
retrieval pipeline. Per §12.8 an evidence snapshot is **not** a live document
link: it captures the body text, source metadata, retrieval scores, the policy
version in effect, and the *source ACL summary* at creation time.

Immutability is enforced at the store boundary: snapshots are written once and
never mutated or deleted through this API. "Immutable" means the recorded facts
do not change — it does **not** mean permanently readable.

Read-time re-authorization (§12.8)
----------------------------------
A snapshot is only as readable as the *current* principal's access to its
source at read time:

* Same tenant + current control-plane ACL grants access + corpus still discoverable
  → ``FULL`` (body returned).
* Source ACL revoked for the caller (e.g. ACL tightened, document deleted,
  tenant changed) → ``REDACTED``: only the provenance metadata is returned,
  the body is withheld.
* Cross-tenant request → ``DENIED`` (the requester must never learn whether
  evidence for another tenant exists).
* A caller holding the independent ``audit:evidence:read`` permission may read
  the body regardless, but this produces an **audit event** (build plan
  §12.8: "具备独立 audit:evidence:read 权限的审计员按保留策略访问并产生审计事件").

Backed by stdlib ``sqlite3`` (no new dependency), consistent with
:mod:`agentic_rag_enterprise.storage.metadata_store`.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Callable, Optional

from agentic_rag_enterprise.domain.document import SourceDocument
from agentic_rag_enterprise.domain.evidence import Evidence
from agentic_rag_enterprise.domain.security import SecurityContext
from agentic_rag_enterprise.security.policy import (
    AuthorizationDecision,
    ResourceAcl,
    can_discover_corpus,
    evaluate_access,
)


class EvidenceAccessLevel(str, Enum):
    FULL = "full"
    REDACTED = "redacted"
    DENIED = "denied"


@dataclass
class EvidenceAccess:
    """Result of an evidence read, carrying the authorization level."""

    level: EvidenceAccessLevel
    evidence: Optional[Evidence]
    reason: str = ""


_AUDIT_PERMISSION = "audit:evidence:read"
CurrentDocumentResolver = Callable[[str, str, str], Optional[SourceDocument]]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _as_json_list(value) -> str:
    if value is None:
        return "[]"
    if isinstance(value, str):
        return value
    return json.dumps(list(value))


def _as_text_list(raw: str) -> list[str]:
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return [str(v) for v in data]


def _redact_evidence(ev: Evidence) -> Evidence:
    """Return a copy with the body withheld but provenance preserved (§12.8)."""
    return ev.model_copy(update={"text": "", "text_hash": ev.text_hash})


class EvidenceSnapshotStore:
    """SQLite-backed immutable evidence snapshot store with read-time auth."""

    def __init__(
        self,
        db_path: str = "evidence.db",
        *,
        audit_callback: Optional[Callable[[dict], None]] = None,
        current_document_resolver: Optional[CurrentDocumentResolver] = None,
    ) -> None:
        self._db_path = db_path
        self._audit_callback = audit_callback
        self._current_document_resolver = current_document_resolver
        self._conn = sqlite3.connect(db_path, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self.apply_migrations()

    # ------------------------------------------------------------------ #
    # Schema
    # ------------------------------------------------------------------ #
    def apply_migrations(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS evidence_snapshots (
                evidence_id        TEXT PRIMARY KEY,
                tenant_id          TEXT NOT NULL,
                corpus_id          TEXT NOT NULL,
                document_id        TEXT NOT NULL,
                document_version   TEXT NOT NULL,
                source_uri         TEXT NOT NULL DEFAULT '',
                source_filename    TEXT NOT NULL DEFAULT '',
                parent_id          TEXT,
                child_chunk_id     TEXT,
                page_number        INTEGER,
                section_path       TEXT NOT NULL DEFAULT '[]',
                start_offset       INTEGER,
                end_offset         INTEGER,
                text               TEXT NOT NULL,
                text_hash          TEXT NOT NULL,
                retrieval_query    TEXT NOT NULL DEFAULT '',
                retrieval_score   REAL,
                rerank_score       REAL,
                authority_level    INTEGER NOT NULL DEFAULT 50,
                effective_from     TEXT,
                effective_to       TEXT,
                deprecated         INTEGER NOT NULL DEFAULT 0,
                retrieved_at       TEXT NOT NULL,
                acl_policy_id      TEXT NOT NULL DEFAULT 'unknown',
                policy_version     TEXT NOT NULL,
                retrieval_iteration INTEGER NOT NULL DEFAULT 0,
                plan_step_id       TEXT,
                security_level     TEXT NOT NULL DEFAULT 'internal',
                acl_scope          TEXT NOT NULL DEFAULT 'restricted',
                allowed_user_ids   TEXT NOT NULL DEFAULT '[]',
                allowed_group_ids  TEXT NOT NULL DEFAULT '[]',
                denied_user_ids    TEXT NOT NULL DEFAULT '[]',
                denied_group_ids   TEXT NOT NULL DEFAULT '[]'
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS evidence_audit_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                evidence_id TEXT NOT NULL,
                tenant_id   TEXT NOT NULL,
                actor_user_id TEXT NOT NULL,
                action      TEXT NOT NULL,
                reason      TEXT NOT NULL DEFAULT '',
                at          TEXT NOT NULL
            )
            """
        )

    # ------------------------------------------------------------------ #
    # Write (immutable)
    # ------------------------------------------------------------------ #
    def save(self, evidence: Evidence, *, source_acl: ResourceAcl) -> None:
        """Persist an evidence snapshot with its source ACL summary.

        Idempotent on ``evidence_id``: re-saving the same id is a no-op, so a
        retried retrieval never duplicates a snapshot. The body is never
        updated once written. ``source_acl`` is the ACL in effect at creation
        time and is retained as audit provenance. Ordinary reads are authorized
        only through ``current_document_resolver`` (build plan §12.8).
        """
        self._conn.execute(
            """
            INSERT OR IGNORE INTO evidence_snapshots (
                evidence_id, tenant_id, corpus_id, document_id, document_version,
                source_uri, source_filename, parent_id, child_chunk_id,
                page_number, section_path, start_offset, end_offset,
                text, text_hash, retrieval_query, retrieval_score, rerank_score,
                authority_level, effective_from, effective_to, deprecated,
                retrieved_at, acl_policy_id, policy_version, retrieval_iteration,
                plan_step_id, security_level, acl_scope,
                allowed_user_ids, allowed_group_ids, denied_user_ids, denied_group_ids
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            (
                evidence.evidence_id,
                evidence.tenant_id,
                evidence.corpus_id,
                evidence.document_id,
                evidence.document_version,
                evidence.source_uri,
                evidence.source_filename,
                evidence.parent_id,
                evidence.child_chunk_id,
                evidence.page_number,
                json.dumps(list(evidence.section_path)),
                evidence.start_offset,
                evidence.end_offset,
                evidence.text,
                evidence.text_hash,
                evidence.retrieval_query,
                evidence.retrieval_score,
                evidence.rerank_score,
                evidence.authority_level,
                evidence.effective_from.isoformat() if evidence.effective_from else None,
                evidence.effective_to.isoformat() if evidence.effective_to else None,
                1 if evidence.deprecated else 0,
                evidence.retrieved_at.isoformat(),
                evidence.acl_policy_id,
                evidence.policy_version,
                evidence.retrieval_iteration,
                evidence.plan_step_id,
                source_acl.security_level,
                source_acl.acl_scope,
                _as_json_list(source_acl.allowed_user_ids),
                _as_json_list(source_acl.allowed_group_ids),
                _as_json_list(source_acl.denied_user_ids),
                _as_json_list(source_acl.denied_group_ids),
            ),
        )

    def exists(self, evidence_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM evidence_snapshots WHERE evidence_id = ?", (evidence_id,)
        ).fetchone()
        return row is not None

    def count(self, tenant_id: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM evidence_snapshots WHERE tenant_id = ?",
            (tenant_id,),
        ).fetchone()
        return int(row["n"]) if row else 0

    # ------------------------------------------------------------------ #
    # Read (re-authorized at read time)
    # ------------------------------------------------------------------ #
    def get(self, evidence_id: str, ctx: SecurityContext) -> EvidenceAccess:
        """Read a snapshot, re-authorizing against the current principal."""
        row = self._conn.execute(
            "SELECT * FROM evidence_snapshots WHERE evidence_id = ?", (evidence_id,)
        ).fetchone()
        if row is None:
            return EvidenceAccess(EvidenceAccessLevel.DENIED, None, "not_found")

        if row["tenant_id"] != ctx.tenant_id:
            return EvidenceAccess(EvidenceAccessLevel.DENIED, None, "cross_tenant")

        evidence = self._row_to_evidence(row)

        current_document = self._resolve_current_document(row)
        if current_document is not None and self._is_allowed_now(
            ctx, self._acl_from_document(current_document), row["corpus_id"]
        ):
            return EvidenceAccess(EvidenceAccessLevel.FULL, evidence, "authorized")

        if _AUDIT_PERMISSION in ctx.permissions:
            self._record_audit(evidence_id, ctx, "audit_read", "audit:evidence:read grant")
            return EvidenceAccess(EvidenceAccessLevel.FULL, evidence, "audit_grant")

        reason = (
            "current_policy_unavailable"
            if self._current_document_resolver is None
            else "source_acl_revoked_or_document_inactive"
        )
        return EvidenceAccess(EvidenceAccessLevel.REDACTED, _redact_evidence(evidence), reason)

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _is_allowed_now(ctx: SecurityContext, acl: ResourceAcl, corpus_id: str) -> bool:
        return evaluate_access(ctx, acl) is AuthorizationDecision.ALLOW and can_discover_corpus(
            ctx, corpus_id
        )

    def _resolve_current_document(self, row: sqlite3.Row) -> Optional[SourceDocument]:
        """Load the current control-plane document; absence fails closed.

        The ACL stored beside a snapshot is historical audit metadata. It cannot
        authorize a later read because ACL tightening or logical deletion may
        have happened since the snapshot was written.
        """
        if self._current_document_resolver is None:
            return None
        document = self._current_document_resolver(
            row["tenant_id"], row["corpus_id"], row["document_id"]
        )
        if document is None or document.status.value != "active" or document.deprecated:
            return None
        return document

    @staticmethod
    def _acl_from_document(document: SourceDocument) -> ResourceAcl:
        return ResourceAcl(
            tenant_id=document.tenant_id,
            security_level=document.security_level,
            acl_scope=document.acl_scope,
            allowed_user_ids=document.allowed_user_ids,
            allowed_group_ids=document.allowed_group_ids,
            denied_user_ids=document.denied_user_ids,
            denied_group_ids=document.denied_group_ids,
        )

    def _record_audit(
        self, evidence_id: str, ctx: SecurityContext, action: str, reason: str
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO evidence_audit_log (
                evidence_id, tenant_id, actor_user_id, action, reason, at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (evidence_id, ctx.tenant_id, ctx.user_id, action, reason, _now_iso()),
        )
        if self._audit_callback is not None:
            self._audit_callback(
                {
                    "evidence_id": evidence_id,
                    "tenant_id": ctx.tenant_id,
                    "actor_user_id": ctx.user_id,
                    "action": action,
                    "reason": reason,
                    "at": _now_iso(),
                }
            )

    @staticmethod
    def _row_to_evidence(row: sqlite3.Row) -> Evidence:
        def _dt(value: Optional[str]) -> Optional[datetime]:
            if not value:
                return None
            return datetime.fromisoformat(value)

        return Evidence(
            evidence_id=row["evidence_id"],
            tenant_id=row["tenant_id"],
            corpus_id=row["corpus_id"],
            document_id=row["document_id"],
            document_version=row["document_version"],
            source_uri=row["source_uri"],
            source_filename=row["source_filename"],
            parent_id=row["parent_id"],
            child_chunk_id=row["child_chunk_id"],
            page_number=row["page_number"],
            section_path=tuple(_as_text_list(row["section_path"])),
            start_offset=row["start_offset"],
            end_offset=row["end_offset"],
            text=row["text"],
            text_hash=row["text_hash"],
            retrieval_query=row["retrieval_query"],
            retrieval_score=row["retrieval_score"],
            rerank_score=row["rerank_score"],
            authority_level=int(row["authority_level"]),
            effective_from=_dt(row["effective_from"]),
            effective_to=_dt(row["effective_to"]),
            deprecated=bool(row["deprecated"]),
            retrieved_at=_dt(row["retrieved_at"]) or datetime.now(timezone.utc),
            acl_policy_id=row["acl_policy_id"],
            policy_version=row["policy_version"],
            retrieval_iteration=int(row["retrieval_iteration"]),
            plan_step_id=row["plan_step_id"],
        )
