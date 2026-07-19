# Issue E-022 — Reconciler + purge + index migration + rollback

**Milestone:** M7 — Runtime hardening (`E-022` → `E-024`)
**Status:** contract open — implementation pending. Acceptance of this doc unlocks
`ingestion/reconciler.py`, the index-migration / active-version-rollback paths in
`DocumentManager` + `MetadataStore`, the per-corpus active-collection pointer in
`corpus/registry.py`, and the `tests/.../test_e022_*` suites.
**Baseline:** `8f91d90` (main HEAD; M6 / E-021 CLOSED / ACCEPTED at `c6c3e6b`; M7 current).
**Build plan refs:** §10.6 (delete semantics), §10.8 (embedding/chunking upgrade → new
collection + alias/registry switch + retain v1 for rollback), §10.10 (cross-store
consistency protocol — esp. point 6 "DB 提交成功后的清理失败由 reconciler 重试，不回滚已经
可见的新版本"), Milestone 7 (§3619 / §3621 / §3623), §5079 (M7 / E-022 scope), §23.7
(quality-gate commands), §29.5 (`depends_on` / `in_scope` / `deferred_to` rules).
**Depends on:** E-008 / E-008.1–E-008.4 (idempotent ingestion + active-version protocol +
atomic `document_builds` lease + fencing + `recover=True` job recovery), E-010 (logical
delete + physical purge + authorization), E-011 (metadata + evidence store), E-015 /
E-016 (corpus/capability registry + permission-aware router — corpus-discoverability truth).
Reuses `storage/metadata_store.py` (source of truth), `storage/vector_store.py`
(Qdrant wrapper), `storage/parent_store.py`, `ingestion/job.py` (`DocumentManager`,
`IngestionJob`), `corpus/registry.py`. **No new storage engine is introduced** — local
SQLite + in-process/local Qdrant only (Postgres / Qdrant Server are M9).

---

## 1. Scope and non-goals

### In scope (runtime hardening of the ingestion / index / lifecycle planes)

- **Standalone `Reconciler`** (`ingestion/reconciler.py`): a deterministic, idempotent,
  fenceable process that treats the **Metadata DB as the sole source of truth** (§10.10) and
  repairs the *rebuildable* data planes (Qdrant points, Parent Store content, parsed/raw
  artifacts, dead-letter jobs) toward that truth. It does **not** decide document lifecycle
  from Qdrant / Parent Store / filesystem.
  - Orphan detection: Qdrant points / Parent chunks whose `(document_id, document_version)`
    has no active/committed row → enqueue reconciler-safe physical purge.
  - Missing data plane: metadata active doc+version with absent Qdrant points / Parent
    content → rebuild (re-run the existing `IngestionJob` for that doc version) or flag.
  - Post-commit cleanup retry (§10.10 #6): cleanup failures after a successful DB commit are
    retried by the reconciler; the already-visible new version is **never** rolled back.
  - Dead-letter / failed jobs (§10.10 #7): idempotent cleanup of uncommitted data-plane
    artifacts left by failed jobs.
  - Corpus-discoverability consistency: ensure the registry pointer matches the persisted
    `corpus_registry` truth.
  - **Dry-run mode + audit table** (`reconciliation_runs` / `reconciliation_findings`): every
    action is observable and replayable; nothing mutates in dry-run.
- **Index migration** (`ingestion/index_migration.py`): build a new index into a **new
  collection** named `corpus_id_v{embedding_version}_{chunking_version}` (§10.8), run alongside
  v1 **without disrupting retrieval on v1**, then switch via an **atomic registry/alias pointer
  flip** under a lease, **retaining v1 for rollback**. Never clear-and-rebuild the production
  collection (§10.8 prohibition).
- **Rollback**:
  - *Index rollback*: flip the per-corpus active-collection pointer back to the retained
    previous collection; the superseded v2 is retained (not deleted) for later purge.
  - *Active-version rollback* (the "临时回滚 v1" capability, build plan §2630): set a
    document's `active_version` back to the previously-active version under a
    `lifecycle_revision` CAS — only allowed if that version still exists (not purged).
- **Purge hardening via reconciler**: physical purge of logically-deleted documents is retried
  to completion by the reconciler; orphaned data-plane artifacts never survive.

### Deferred to sibling issues (do NOT pre-build)

- **E-023** — persistent checkpoint + re-authorization on resume. E-022 does **not** add any
  agent-run checkpoint, resume token, or re-auth layer.
- **E-024** — readiness + cancellation + backup/restore + runbooks. E-022 does **not** add
  `/health`/`/ready` endpoints, cancellation tokens, or backup/restore jobs.
- **M9** — real Postgres, Qdrant Server, SSO, external connectors, online monitoring,
  canary. E-022 stays on local SQLite + in-process/local Qdrant.

### Forbidden / non-goals

- **No LLM / NLP in the reconciler or index migration.** Both are deterministic and hermetic.
- **No new model download / external API in tests** — use the existing `fake` encoders and
  local Qdrant; tests must be fully hermetic.
- **No "预留接口" for E-023 / E-024** — do not add unused checkpoint/backup services, tables,
  or runtime branches "for later". Minimal type boundaries only, and only if exercised by a
  current test.
- **No clear-and-rebuild of the production collection** (§10.8). Migration always writes a new
  collection and switches a pointer.
- **Reconciler must never resurrect deleted/purged evidence** — it only deletes data-plane
  artifacts for `(document_id, document_version)` that is absent from, or logically deleted in,
  the metadata truth. It must not reintroduce a logically-deleted or purged document.
- **No change to the Planner core** (`planner/`, `executor.py`, `result.py`, `budget.py`,
  `tool_registry.py`) — these are frozen and out of scope for M7's E-022.

### Hard invariants (frozen)

1. **Metadata DB is the sole source of truth.** Reconciler, index migration, and rollback
   never infer lifecycle from Qdrant / Parent Store / filesystem (§10.10).
2. **Reconciler deletes from data planes ONLY for doc+version absent from truth** (or
   logically deleted past retention); it never reintroduces deleted/purged evidence.
3. **Index switch is atomic via registry pointer + lease**; v1 is retained until an explicit
   post-rollback purge; the production collection is never cleared-and-rebuilt (§10.8).
4. **Active-version rollback requires the target version to still exist** (not purged) and
   wins via `lifecycle_revision` CAS; it never overwrites a newer committed revision.
5. **Reconciler is deterministic, idempotent, and fenceable** — safe to run repeatedly and
   concurrently (single active reconciler per corpus via a lease/lock).
6. **No E-023 / E-024 overlap** — no checkpoint, re-auth, readiness, cancellation, or
   backup/restore in this issue.

---

## 2. `Reconciler` design

```python
# ingestion/reconciler.py
class Reconciler:
    def __init__(self, metadata_store, vector_store, parent_store,
                 document_manager, corpus_registry, *, dry_run: bool = False):
        ...

    def reconcile_corpus(self, corpus_id: str) -> ReconciliationReport:
        """Scan metadata truth for corpus_id; repair data planes toward it."""

    def reconcile_all(self) -> ReconciliationReport:
        ...
```

- **Source-of-truth scan**: read `documents` / `chunks` / `document_builds` / `ingestion_jobs`
  from `MetadataStore`. Build the set of *expected* `(document_id, document_version)` data-plane
  artifacts for `status=active` rows.
- **Data-plane scan**: enumerate Qdrant point ids per corpus (`VectorStore.list_point_ids_by_document`),
  Parent Store chunks, and parsed/raw artifact references.
- **Findings** (recorded in `reconciliation_findings`, surfaced in `ReconciliationReport`):
  - `orphan_qdrant_point` / `orphan_parent_chunk` — data-plane artifact with no truth row →
    reconciler-safe purge (respects the logical-delete-first precondition; never purges an
    active doc).
  - `missing_qdrant_point` / `missing_parent_chunk` — active truth row with missing data plane
    → rebuild via `DocumentManager`/`IngestionJob` rerun for that doc version (idempotent).
  - `post_commit_cleanup_failure` — §10.10 #6: retry cleanup of the already-committed new
    version; **do not roll back** the new version.
  - `dead_letter_orphan` — failed job with uncommitted data plane → idempotent cleanup (#7).
  - `registry_mismatch` — registry pointer disagrees with `corpus_registry` truth → realign.
- **Safety**: every mutation is gated by a per-corpus lease; `dry_run=True` records findings
  and mutates nothing. Reconciler never *creates* a document version or *activates* a version —
  it only repairs data planes and aligns pointers.
- **Fencing**: a single active reconciler per corpus (lease/lock in `MetadataStore`); concurrent
  `reconcile_*` calls for the same corpus are serialized.

## 3. Index migration (`index_migration.py`)

```python
# ingestion/index_migration.py
def build_index_v2(corpus_id, *, embedding_model, chunking_version,
                   document_manager) -> IndexBuild:
    """Ingest active docs into NEW collection corpus_id_v{emb}_{chunk}. No v1 disruption."""

def switch_index(corpus_id, *, target_collection: str, dry_run: bool = False) -> None:
    """Atomically flip the per-corpus active-collection pointer under a lease; retain v1."""

def rollback_index(corpus_id) -> None:
    """Flip pointer back to the retained previous collection; v2 kept (superseded, not deleted)."""
```

- **New collection, never in-place**: v2 is written to `corpus_id_v{embedding_version}_{chunking_version}`
  (§10.8). Retrieval on v1 continues unaffected during the build.
- **Switch = pointer flip**: the per-corpus active collection is recorded in `corpus/registry.py`
  (and persisted in `corpus_registry`). `switch_index` flips it atomically under a lease,
  observes, and **retains v1** for rollback. Mirrors the build-plan flow
  `build v2 → offline eval → shadow retrieval → switch pointer → observe → retain v1 → purge later`.
- **Rollback of index**: `rollback_index` flips the pointer back; v2 is retained (superseded)
  until an explicit later purge — never auto-deleted.
- **Records**: an `index_builds` / `index_collections` table tracks `{corpus_id, collection,
  embedding_version, chunking_version, status (building/active/superseded), previous_collection}`
  so rollback always knows the retained prior collection.

## 4. Active-version rollback

- `DocumentManager.rollback_active_version(document_id, *, to_version: str | None = None)`:
  sets `active_version` back to the previously-active version (the `previous_active_version`
  bound to the lease from E-008.4, or an explicit `to_version`) **only if that version still
  exists** (not purged) and wins via `lifecycle_revision` CAS.
- This is the "临时回滚 v1" operational escape hatch (build plan §2630). It is **forward-safe**:
  a newer committed revision cannot be overwritten by a stale rollback attempt.
- Index pointer and active-version pointer are rolled back **independently** (a doc can be
  rolled back while its corpus index stays on v2, and vice-versa); reconciler keeps the two
  consistent when run.

## 5. Purge hardening (reconciler-driven)

- Physical purge of a logically-deleted document (`DocumentManager.purge`, which already refuses
  non-`DELETED` docs) is retried to completion by the reconciler when its post-commit cleanup
  fails (§10.10 #6). The new version stays visible; only the *cleanup* is retried.
- Orphan data-plane artifacts (no truth row, or logically deleted past retention) are physically
  removed by the reconciler, never resurrected.

## 6. Integration boundary (no sibling overlap)

- Reconciler / index migration / rollback operate **only** on the ingestion + storage + corpus
  layers. They do **not** touch the Planner, the `ChatService` answer pipeline, the evidence/
  temporal/conflict stages (M6), or agent-run state (E-023).
- No readiness/cancellation/backup surface (E-024). A `scripts/rebuild_index.py` entry point
  (referenced but absent in the build plan, §4157) may be added as the operational wrapper for
  `reconcile_corpus` + `build_index_v2` + `switch_index`, but it carries no new service boundary.

## 7. Modifiable paths + migrations

- **New:** `src/agentic_rag_enterprise/ingestion/reconciler.py`,
  `src/agentic_rag_enterprise/ingestion/index_migration.py`, plus
  `scripts/rebuild_index.py` / `scripts/reconcile.py` operational wrappers.
- **Modify:** `storage/metadata_store.py` (add `reconciliation_runs` / `reconciliation_findings`
  tables; add `index_builds` / `index_collections` tracking; add `rollback_active_version` and a
  reconciler lease); `corpus/registry.py` (per-corpus active-collection pointer, persisted);
  `storage/vector_store.py` (collection existence check / list collections if needed for v2
  build); `ingestion/job.py` (`DocumentManager`: expose reconciler-safe purge + explicit
  `rollback_active_version`).
- **Migrations:** `010_e022_reconciliation.sql`, `011_e022_index_collections.sql` (or equivalent),
  applied atomically through the existing `schema_migrations` mechanism.
- **Reuse, no change:** `domain/ingestion.py` (`DocumentStatus`, `DOCUMENT_LIFECYCLE`),
  `domain/document.py`, `domain/chunk.py`, the existing `MetadataStore` query/lease primitives,
  `VectorStore.delete`/`update_payload`/`list_point_ids_by_document`/`search`, `ParentStore`
  delete/deprecate, `DocumentManager.delete`/`purge`/`_compensate`.

## 8. MVP acceptance matrix

| # | Scenario | Expected |
|---|---|---|
| 1 | Orphaned Qdrant point (no metadata row) | Reconciler detects + purges it; dry-run reports without mutating |
| 2 | Active doc with missing data plane | Reconciler detects + rebuilds; retrieval works after |
| 3 | Repeated / concurrent reconcile | Idempotent; fenced to one active reconciler per corpus |
| 4 | Post-commit cleanup failure (§10.10 #6) | Retried by reconciler; new version stays visible (not rolled back) |
| 5 | Index migration v2 | Built alongside v1; v1 retrieval unaffected during build |
| 6 | Switch index | Pointer flips atomically; retrieval hits v2; v1 retained |
| 7 | Rollback index | Pointer flips back to v1; v2 retained (superseded, not deleted) |
| 8 | Rollback active version | Doc returns to previous active version; newer revision protected by CAS |
| 9 | Authorization | Reconciler/rollback respect ACL; no leakage of deleted/scoped content |
| 10 | Crash-point | Reconcile/switch interruptible + resumable via lease + findings table |

## 9. Quality gates (implementation)

```bash
ruff check .
ruff format --check .
mypy src/agentic_rag_enterprise
pytest -q tests/unit
pytest -q tests/integration
pytest -q tests/security
python scripts/rebuild_index.py --dry-run --corpus <id>   # reconciler dry-run smoke
```

New/extended test paths (all hermetic — local SQLite + local Qdrant + `fake` encoders):
- `tests/unit/test_reconciler.py` (orphan/missing/dead-letter detection, dry-run, idempotency,
  fencing).
- `tests/unit/test_index_migration.py` (v2 build alongside v1, pointer switch, rollback).
- `tests/integration/test_e022_reconciler_e2e.py` (reconcile after injected orphans / missing
  data plane; post-commit cleanup retry).
- `tests/integration/test_e022_index_migration_rollback.py` (build → switch → rollback →
  retained-v1 retrieval).
- `tests/security/test_e022_authorization.py` (reconciler/rollback ACL safety, no resurrection
  of deleted evidence).

### Contract-open boundary

This doc is the **issue-opening contract** for E-022. Implementation opens **after** acceptance.
No source is changed by this commit; only `docs/issue-e022-contract.md` is added and the
`AGENTS.md` E-022 allowed-changes placeholder is filled in. Per §29.5, `depends_on` / `in_scope`
/ `deferred_to` are explicit and no deferred capability is pre-built as a reserved interface.
