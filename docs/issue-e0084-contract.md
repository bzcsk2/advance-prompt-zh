# Issue E-008.4 — Audit-remediation of E-008.3 (state-transition gaps)

**Milestone:** M1 — Secure single-corpus data vertical slice
**Status:** CLOSED at this commit
**Parent issue:** E-008.3 (CLOSED at `b7012cb`) — audit verdict **Conditional Fail**
**Baseline preserved:** `b7012cb` is kept intact; E-008.4 is a narrow fix commit.

## Scope

Strict subset of the E-008.3 allowed paths:
`src/agentic_rag_enterprise/{storage,ingestion}/`,
`tests/{unit,integration}/`, `migrations/`, `AGENTS.md`,
`docs/issue-e0084-contract.md`.

No upstream modifications. No `config.py` / `domain/` changes. No new behaviors
beyond the three P1 state-transition gaps below.

## Audit findings (from the E-008.3 code-level review)

1. **P1-1 — Deprecated/superseded version re-delivery is not idempotent.**
   `run()` only short-circuits to `ALREADY_INDEXED` when the existing
   `documents.status == ACTIVE`. A `DEPRECATED` (superseded) version
   re-delivered with identical `content_hash` falls through, takes the lease,
   rewrites the data plane, then fails `_step_verify` (only `processing`/
   `failed`/`active` are allowed) and compensates — **deleting the superseded
   version's Parent/Qdrant/Chunk data plane** (build plan §10.4 violated:
   same `document_id`+`version`+content should be skipped, no duplicate
   Chunks/vectors).
2. **P1-2 — `previous_active_version` is lost across a post-commit takeover.**
   The field lived only on `ingestion_jobs` (per-job). A replacement Job
   `j2` taking over a post-commit-failed `j1` re-queries the current active
   version (now already `v2`) and sets `j2.previous_active_version = v2`,
   so `publish` sees `previous == current` and returns without deprecating the
   truly-replaced `v1` data plane.
3. **P1-3 — Same-`job_id` concurrent delivery can still corrupt the data plane.**
   Different-`job_id` concurrency is mutually exclusive via `BuildConflict`,
   but a same-`job_id` re-entry was treated as a same-owner resume that
   advanced `lease_generation`. Fencing only guarded commit/publish/compensate,
   not the Parent/Qdrant writes. An interleave (Worker A paused in
   `_step_write_qdrant`; Worker B same `job_id` completes + commits + publishes
   + finalizes; Worker A resumes and overwrites the same deterministic Point
   IDs with `processing` payload) leaves Metadata `active` but Qdrant
   `processing` → empty retrieval. The job identity was not distinguished from
   the execution attempt (lease holder).

## Remediation

### P1-1 — Deprecated-version idempotency
- `IngestionJob.run` now returns `ALREADY_INDEXED` for an existing version
  with a matching `content_hash` when its status is `ACTIVE` **or**
  `DEPRECATED` (build plan §10.4). No lease claim, no data-plane write,
  no compensation — the superseded version's data plane is preserved.
- `processing` / `failed` / `active`-but-not-fully-published same-content
  re-delivery still falls through to `run` (resume), so a build stuck between
  commit and publish is finished rather than short-circuited.
- The `job_id` immutable-binding guard is preserved (a reused `job_id` bound
  to a different request fails closed with `JobIdentityConflict`). Factored into
  `_short_circuit_already_indexed()`.

### P1-2 — `previous_active_version` bound to the build lease
- New column `document_builds.previous_active_version`
  (migration `006_e0084_lease_previous_version.sql`).
- `MetadataStore.acquire_job` captures the replaced version at the **first** lease
  claim (current active version) and carries it **forward** on takeover/resume —
  it is **never** recomputed against the (already-switched) active version.
- `publish` reads the persisted `previous_active_version`; a replacement job
  taking over a post-commit-failed build now inherits the true replaced version
  (`v1`) and deprecates its data plane correctly.
- **Upgrade backfill:** migration `006` ends with a one-time `UPDATE
  document_builds SET previous_active_version = (SELECT
  ingestion_jobs.previous_active_version ...) WHERE previous_active_version IS NULL`,
  so an E-008.3 database upgraded in place copies the per-job replaced version
  onto the (already-claimed) lease it owns. Without it, a post-commit-failed
  build taken over after upgrade would recompute the replaced version against the
  already-switched active version and fail to clean the true prior data plane.

### P1-3 — Execution attempt is DB-backed (cross-process serialization)
- `document_builds` gains `attempt_id TEXT` + `claimed_at TEXT`
  (migration `007_e0084_build_attempt.sql`). Each `run()` mints a fresh
  `attempt_id` (uuid) and `acquire_job` persists it on every
  claim/takeover/resume.
- A same-`job_id` re-acquire while the lease is **still `running`** with a
  **different `attempt_id`** is a duplicate delivery (e.g. a second process
  that re-delivered the same `job_id`), NOT a recovery: `acquire_job` raises
  `BuildConflict` and does **not** advance the fencing generation, so the
  in-flight attempt keeps its authority over the deterministic data plane. This
  closes the cross-process race the E-008.3 review left open.
- **Explicit recovery** is required for a same-`job_id` re-acquire on a live
  lease: `run(recover=True)` / `DocumentManager.ingest(recover=True)` passes
  `recover=True` to `acquire_job`, which advances the generation and resumes.
  Implicit (non-recover) re-acquire of a RUNNING same-`job_id` lease is
  rejected — the contract no longer auto-interprets any same-`job_id` re-call
  as a recovery. A **terminal** (failed/succeeded/cancelled) same-`job_id`
  lease is a safe recovery and resumes without `recover=True`.
- The in-process guard (`_claim_build_guard` / `_release_build_guard`) is
  retained as an extra layer for genuine same-process concurrency; the DB-level
  `attempt_id` is the authoritative execution-attempt owner. Cross-process
  **liveness** (detecting a crashed attempt that never released the lease) still
  requires a lease timeout/heartbeat and is explicitly out of scope.

## Acceptance tests
- `tests/unit/test_metadata_store.py`:
  - `test_build_attempt_rejects_duplicate_execution_for_same_job_id` (P1-3
    DB-level attempt: duplicate RUNNING same-`job_id` rejected; `recover=True`
    advances).
  - `test_migration_006_backfills_previous_version_on_upgrade` (P1-2 upgrade
    backfill + post-upgrade takeover inherits the true replaced version).
- `tests/integration/test_e008_crash_points.py`:
  - `test_deprecated_version_redelivery_is_idempotent` (P1-1)
  - `test_takeover_after_publish_failure_keeps_true_previous_version` (P1-2)
  - `test_same_job_id_concurrent_delivery_is_serialized` (P1-3 in-process)
- `tests/baseline/` MUST remain green.
- `ruff`, `mypy src/agentic_rag_enterprise`, full `pytest` all green.

## Non-regression notes
- E-008.3 fixes retained and valid: claim-before-mutate + `BuildConflict`
  never compensates (P1-1), lease fencing + terminal-state sync (P1-2),
  exact Qdrant payload verify (P1-3), precise commit-crash hook (P2).
- E-008.2 fixes retained: migration atomicity (P1-6), mandatory
  `metadata_store` active-version gate (P1-7), commit idempotent resume (P1-1),
  `publish` scopes parents to `self._parents_list` (P2-2), `ALREADY_INDEXED`
  job-row guard (P2-1).
- The P1-3 DB-level `attempt_id` now closes cross-process duplicate delivery;
  a lease timeout/heartbeat remains the production-grade completion of liveness
  detection and is explicitly out of scope for this fix.
