"""Reconciler (E-022, build plan §10.10).

A deterministic, idempotent, fenceable process that treats the **Metadata DB as
the sole source of truth** and repairs the *rebuildable* data planes (Qdrant
points, Parent Store chunks, dead-letter jobs) toward that truth. It never
decides document lifecycle from Qdrant / Parent Store / filesystem, and it never
resurrects a logically-deleted or purged document.

Repair modes
------------
* **Orphan data plane** — Qdrant points / parent chunks whose
  ``(document_id, document_version)`` has *no* Metadata DB row at all are
  physically removed (genuine leftovers from a crashed pre-commit build).
* **Missing data plane** — an ``active`` document version with no Qdrant points
  is flagged and, when a ``rebuild_document`` callback is supplied, rebuilt.
* **Post-commit cleanup retry (§10.10 #6)** — a logically-``deleted`` document
  whose data plane still lingers is physically purged via the ``purge_document``
  callback; the already-visible active version is never rolled back.
* **Dead-letter jobs** — ``failed`` ingestion jobs may be cleaned up via the
  optional ``compensate_job`` callback.

All mutations are recorded as findings and are skipped entirely in ``dry_run``.
A per-corpus lease (``MetadataStore.acquire_reconciler_lease``) guarantees at
most one active reconciler per corpus.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional

from agentic_rag_enterprise.corpus.registry import CorpusRegistry
from agentic_rag_enterprise.storage.metadata_store import MetadataStore
from agentic_rag_enterprise.storage.parent_store import ParentStore
from agentic_rag_enterprise.storage.vector_store import VectorStore

# Callbacks the production wiring supplies; tests may pass ``None`` (findings
# only, no rebuild/purge side effects).
RebuildFn = Callable[[str, str, str, str], None]  # (tenant, corpus, doc, ver)
PurgeFn = Callable[[str, str, str], None]  # (tenant, corpus, doc)
CompensateFn = Callable[[str], None]  # (job_id)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class ReconciliationFinding:
    kind: str
    corpus_id: str
    tenant_id: Optional[str] = None
    document_id: Optional[str] = None
    document_version: Optional[str] = None
    detail: str = ""


@dataclass
class ReconciliationReport:
    corpus_id: Optional[str]
    findings: list[ReconciliationFinding] = field(default_factory=list)
    mutated: bool = False
    run_id: str = ""
    started_at: str = ""
    finished_at: str = ""

    def __post_init__(self) -> None:
        if not self.started_at:
            self.started_at = _now_iso()

    def add(
        self,
        kind: str,
        corpus_id: str,
        *,
        tenant_id: Optional[str] = None,
        document_id: Optional[str] = None,
        document_version: Optional[str] = None,
        detail: str = "",
    ) -> None:
        self.findings.append(
            ReconciliationFinding(
                kind=kind,
                corpus_id=corpus_id,
                tenant_id=tenant_id,
                document_id=document_id,
                document_version=document_version,
                detail=detail,
            )
        )


class Reconciler:
    """Reconcile a corpus's data planes toward Metadata DB truth."""

    def __init__(
        self,
        metadata_store: MetadataStore,
        vector_store: VectorStore,
        parent_store: ParentStore,
        corpus_registry: CorpusRegistry,
        *,
        dry_run: bool = False,
        owner: Optional[str] = None,
        rebuild_document: Optional[RebuildFn] = None,
        purge_document: Optional[PurgeFn] = None,
        compensate_job: Optional[CompensateFn] = None,
    ) -> None:
        self._store = metadata_store
        self._vector = vector_store
        self._parents = parent_store
        self._registry = corpus_registry
        self._dry_run = dry_run
        self._owner = owner or f"reconciler-{uuid.uuid4().hex[:8]}"
        self._rebuild = rebuild_document
        self._purge = purge_document
        self._compensate = compensate_job

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    def reconcile_corpus(self, corpus_id: str) -> ReconciliationReport:
        report = ReconciliationReport(corpus_id=corpus_id)
        report.run_id = uuid.uuid4().hex
        if not self._store.acquire_reconciler_lease(corpus_id, self._owner):
            # Another reconciler owns this corpus; record a no-op run.
            report.add("lease_busy", corpus_id, detail="another reconciler holds the lease")
            self._store.begin_reconciliation_run(
                report.run_id, corpus_id, report.started_at, dry_run=self._dry_run
            )
            self._store.finish_reconciliation_run(report.run_id, _now_iso(), 0, mutated=False)
            report.finished_at = report.started_at
            return report

        try:
            self._store.begin_reconciliation_run(
                report.run_id, corpus_id, report.started_at, dry_run=self._dry_run
            )
            self._reconcile(corpus_id, report)
            report.finished_at = _now_iso()
            self._store.finish_reconciliation_run(
                report.run_id,
                report.finished_at,
                len(report.findings),
                mutated=report.mutated,
            )
        finally:
            self._store.release_reconciler_lease(corpus_id, self._owner)
        return report

    def reconcile_all(self) -> list[ReconciliationReport]:
        return [self.reconcile_corpus(cid) for cid in self._registry.list_corpus_ids()]

    # ------------------------------------------------------------------ #
    # Core
    # ------------------------------------------------------------------ #
    def _reconcile(self, corpus_id: str, report: ReconciliationReport) -> None:
        # --- Registry pointer realign (E-022 contract P1-4) ------------ #
        # The persisted pointer (MetadataStore) is the source of truth; the live
        # in-memory registry is a derived mirror. If they have drifted (e.g. a
        # restart re-seeded the registry without the migrated pointer), realign
        # the live registry to the persisted value BEFORE scanning, so the rest
        # of the reconcile operates on the correct collection.
        persisted_collection = self._store.get_active_collection(corpus_id)
        live_collection = self._registry.resolve_collection_name(corpus_id)
        effective_persisted = persisted_collection or corpus_id
        if live_collection != effective_persisted:
            report.add(
                "registry_mismatch",
                corpus_id,
                detail=f"live registry={live_collection!r} persisted={effective_persisted!r}",
            )
            if not self._dry_run:
                self._registry.set_active_collection(corpus_id, effective_persisted)
                report.mutated = True

        collection = self._registry.resolve_collection_name(corpus_id)

        active = set(self._store.iter_active_document_versions(corpus_id))
        known = set(self._store.iter_known_document_versions(corpus_id))
        deleted = set(self._store.iter_deleted_document_ids(corpus_id))

        # --- Qdrant orphan + missing detection -------------------------- #
        points = (
            self._vector.scroll_all(collection)
            if self._vector.collection_exists(collection)
            else []
        )
        qdrant_present: set[tuple[str, str, str]] = set()
        for point_id, payload in points:
            p_tenant = str(payload.get("tenant_id", ""))
            doc = str(payload.get("document_id", ""))
            ver = str(payload.get("document_version", ""))
            key = (p_tenant, doc, ver)
            qdrant_present.add(key)
            if key not in known:
                report.add(
                    "orphan_qdrant_point",
                    corpus_id,
                    tenant_id=p_tenant,
                    document_id=doc,
                    document_version=ver,
                    detail=f"point {point_id} has no metadata row",
                )
                if not self._dry_run:
                    self._vector.delete(collection, [point_id])
                    report.mutated = True
                continue
            # Stale data-plane status: the Metadata DB is the truth. A point for
            # an active version must read active; a point for a non-active
            # (deprecated/deleted) version must read deprecated. This keeps
            # retrieval (status==active AND deprecated==false) consistent with the
            # control plane after an active-version rollback (E-022 contract P1-2).
            point_status = payload.get("status")
            if key in active and point_status != "active":
                report.add(
                    "stale_qdrant_status",
                    corpus_id,
                    tenant_id=p_tenant,
                    document_id=doc,
                    document_version=ver,
                    detail=f"point {point_id} should be active (metadata active)",
                )
                if not self._dry_run:
                    self._vector.update_payload(
                        collection, [point_id], {"status": "active", "deprecated": False}
                    )
                    report.mutated = True
            elif key not in active and point_status == "active":
                report.add(
                    "stale_qdrant_status",
                    corpus_id,
                    tenant_id=p_tenant,
                    document_id=doc,
                    document_version=ver,
                    detail=f"point {point_id} should be deprecated (metadata non-active)",
                )
                if not self._dry_run:
                    self._vector.update_payload(
                        collection, [point_id], {"status": "deprecated", "deprecated": True}
                    )
                    report.mutated = True

        for t_id, doc, ver in sorted(active):
            if (t_id, doc, ver) not in qdrant_present:
                report.add(
                    "missing_qdrant_point",
                    corpus_id,
                    tenant_id=t_id,
                    document_id=doc,
                    document_version=ver,
                    detail="active version has no Qdrant points",
                )
                if self._rebuild is not None and not self._dry_run:
                    self._rebuild(t_id, corpus_id, doc, ver)
                    report.mutated = True
                continue
            # P1-4: an active version whose Qdrant points exist but whose Parent
            # Store entries are missing. The Parent Store is in-memory and not
            # persisted, so a crash/restart can leave it empty while Qdrant is
            # intact. Flag it and rebuild (DocumentManager.rebuild_document also
            # recreates the parents from the child chunk rows).
            if not self._parents.list_parent_ids(t_id, corpus_id, doc, ver):
                report.add(
                    "missing_parent_chunk",
                    corpus_id,
                    tenant_id=t_id,
                    document_id=doc,
                    document_version=ver,
                    detail="active version has Qdrant points but no Parent Store entries",
                )
                if self._rebuild is not None and not self._dry_run:
                    self._rebuild(t_id, corpus_id, doc, ver)
                    report.mutated = True

        # --- Post-commit cleanup retry: purge lingering deleted docs --- #
        for t_id, doc, ver in sorted(deleted):
            point_ids = (
                self._vector.list_point_ids_by_document(collection, t_id, corpus_id, doc, ver)
                if self._vector.collection_exists(collection)
                else []
            )
            parent_ids = self._parents.list_parent_ids(t_id, corpus_id, doc, ver)
            if point_ids or parent_ids:
                report.add(
                    "post_commit_cleanup_failure",
                    corpus_id,
                    tenant_id=t_id,
                    document_id=doc,
                    document_version=ver,
                    detail=f"{len(point_ids)} qdrant points / {len(parent_ids)} parents linger",
                )
                if self._purge is not None and not self._dry_run:
                    self._purge(t_id, corpus_id, doc)
                    report.mutated = True

        # --- Parent-store orphan detection ----------------------------- #
        for parent_id, p_tenant, p_corpus, p_doc, p_ver in self._parents.iter_all_parents():
            if p_corpus != corpus_id:
                continue
            if (p_tenant, p_doc, p_ver) not in known:
                report.add(
                    "orphan_parent_chunk",
                    corpus_id,
                    tenant_id=p_tenant,
                    document_id=p_doc,
                    document_version=p_ver,
                    detail=f"parent {parent_id} has no metadata row",
                )
                if not self._dry_run:
                    self._parents.delete(parent_id)
                    report.mutated = True
                continue
            # Stale parent status: mirror the Qdrant stale-status check. A parent
            # for an active version must be active; a parent for a non-active
            # version must be deprecated, so the ParentReader's second-auth
            # (status==active AND deprecated==false) agrees with the control plane.
            parent = self._parents.get(parent_id)
            p_status = parent.metadata.get("status") if parent is not None else None
            p_dep = parent.metadata.get("deprecated") if parent is not None else None
            if (p_tenant, p_doc, p_ver) in active:
                if p_status != "active" or p_dep:
                    report.add(
                        "stale_parent_status",
                        corpus_id,
                        tenant_id=p_tenant,
                        document_id=p_doc,
                        document_version=p_ver,
                        detail=f"parent {parent_id} should be active (metadata active)",
                    )
                    if not self._dry_run:
                        self._parents.activate_document(p_tenant, p_corpus, p_doc, p_ver)
                        report.mutated = True
            else:
                if p_status == "active" and not p_dep:
                    report.add(
                        "stale_parent_status",
                        corpus_id,
                        tenant_id=p_tenant,
                        document_id=p_doc,
                        document_version=p_ver,
                        detail=f"parent {parent_id} should be deprecated (metadata non-active)",
                    )
                    if not self._dry_run:
                        self._parents.deprecate_document(p_tenant, p_corpus, p_doc, p_ver)
                        report.mutated = True

        # --- Dead-letter jobs ------------------------------------------ #
        for job_id in self._store.iter_failed_job_ids(corpus_id):
            report.add(
                "dead_letter_orphan",
                corpus_id,
                detail=f"failed job {job_id} may have uncommitted data plane",
            )
            if self._compensate is not None and not self._dry_run:
                self._compensate(job_id)
                report.mutated = True

        # Persist every finding for auditability.
        for f in report.findings:
            self._store.record_reconciliation_finding(
                report.run_id,
                kind=f.kind,
                corpus_id=f.corpus_id,
                tenant_id=f.tenant_id,
                document_id=f.document_id,
                document_version=f.document_version,
                detail=f.detail,
            )
