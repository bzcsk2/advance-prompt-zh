# Issue E-024 — Health/readiness, persistent cancellation, backup/restore + runbooks

**Milestone:** M7 — Runtime hardening (`E-022` → `E-024`)
**Status:** contract open — implementation pending. This is the **remediation** of the
first submission (`ea3c724`), which was returned FAIL on six blocking (P1) design gaps.
All six P1 findings (P1-1 storage topology, P1-2 round-0 cancellation, P1-3 CAS method,
P1-4 cancel API shape, P1-5 health route migration, P1-6 readiness collection scope)
are closed below. Acceptance of this doc unlocks, in order, the readiness/liveness
probe (reusing the existing `/health` + new `/ready`), the persistent cancellation
surface (built on the E-023 `run_checkpoints` state machine, with a new CAS method in
`storage/metadata_store.py`), the backup/restore tooling (multi-file SQLite snapshot
covering BOTH the Metadata DB and the Evidence Snapshot Store + restore-through-
reconciler), and the operations runbooks under `docs/runbooks/`.
**Baseline:** `1bc627f` (main HEAD; E-023 CLOSED / ACCEPTED at `a1cd282`; M7 current).
**Build plan refs:** Milestone 7 (§3619 / §3621 / §3623 — exit gate: "依赖故障降级和恢复
测试通过；冻结 release reference profile"), §4214 (FastAPI runtime hardening list —
"持久 Checkpoint / Health / readiness / Cancel / Backend failure degradation"), §5081
(M7 / E-024 scope), §23.7 (quality-gate commands), §29.5 (`depends_on` / `in_scope` /
`deferred_to` rules).
**Depends on:** E-008 / E-008.x (MetadataStore = control-plane source of truth +
`document_builds` lease pattern), E-011 (Evidence Snapshot Store — `evidence.db`,
immutable evidence snapshots + `evidence_audit_log`, reference replay + read-time
re-auth audit), E-022 (Reconciler + index rollback — the rebuild tool E-024 restore
leans on), E-023 (`run_checkpoints` state machine = the ONLY cancellation truth source).
Reuses `storage/metadata_store.py`, `storage/evidence_store.py` (`EvidenceSnapshotStore`),
`storage/checkpoint_store.py` (`RunCheckpoint`, `CHECKPOINT_RUNNING`/`COMPLETED`/`ABORTED`),
`services/chat_service.py` (`resume_run`, the iteration loop), `corpus/registry.py`,
`ingestion/reconciler.py`. **No new storage engine is introduced** — local SQLite +
in-process/local Qdrant only (Postgres / Qdrant Server are M9).

### Frozen storage topology (P1-1)

The Internal MVP has **TWO** SQLite control-plane stores, both on the local filesystem
(verified against `src/.../storage/evidence_store.py:101` and
`src/.../services/container.py`):

* **Metadata DB** (`metadata.db` by default, `settings.metadata_db_path`) — documents,
  ACLs, corpus registry, active-version pointers, ingestion leases, checkpoint table,
  reconciler findings.
* **Evidence Snapshot Store** (`evidence.db` by default, `EvidenceSnapshotStore`) —
  immutable `evidence_snapshots` rows + `evidence_audit_log` (`audit:evidence:read`
  events). This is the E-011 reference-replay + audit source of truth; it is a
  SEPARATE file from the Metadata DB and is NOT wired into `DefaultServiceContainer`
  today.

The Qdrant collection(s) and the Parent Store remain **rebuildable data planes** (the
Reconciler / re-ingest reconstruct them from the Metadata DB). The Evidence Snapshot
Store, however, is NOT rebuildable from the Metadata DB (it carries historical body +
audit events), so it is a **backup source of truth**, not a rebuildable plane.

Therefore the backup scope MUST cover both DB files (see §1-C). A single-file
`metadata.db`-only backup is explicitly **rejected** by this contract.

---

## 1. Scope and non-goals

### In scope (runtime hardening of the serving / operations planes)

- **A. Health & Readiness** (`api/routes/health.py`):
  - `GET /health` (liveness): answers only "is the process alive?" — does NOT depend on
    Qdrant / Metadata DB / model availability, so a transient dependency fault never
    kills liveness.
  - `GET /ready` (readiness): answers "can this instance safely take prod traffic?"
    Checks (each with a strict timeout): applied migrations, Metadata DB reachable,
    Corpus Registry loaded, at least one active collection present, required local
    dependencies present. Any failure → **503** with a fixed, generic body.
  - readiness **never** calls the LLM and **never** performs a business-side repair;
    the `Reconciler` is the repair tool, readiness is a diagnostic gate only.
  - Strict per-check timeouts; a dependency exception → 503; the client sees only
    controlled status — never a path, SQL text, corpus name, or exception body.
- **B. Persistent Cancellation** (built on E-023 `run_checkpoints`):
  - `running → aborted` via a new `cancel_run(run_id, ctx)` service method reached by a
    dedicated `POST /v1/runs/{run_id}/cancel` endpoint (see §2 "Cancellation API").
    NO in-memory-only cancellation token is ever the source of truth.
  - Re-validates `tenant_id` / `user_id` / `session_id` binding before cancelling;
    foreign principal → fail closed (refused, indistinguishable from "not found").
  - Uses a new atomic CAS method `cancel_run_checkpoint(...)` in
    `storage/metadata_store.py` so `complete` and `cancel` cannot race into an illegal
    state (P1-3). Idempotent: cancelling an already-`aborted` run returns `already_aborted`.
  - **Round-0 cancellation (P1-2):** the loop MUST create a minimal `running` checkpoint
    *before* the round-0 `run_fast_path` retrieval, so a `run_id` submitted by the
    client is cancellable even while the first retrieval is blocking. The pre-round-0
    checkpoint uses `first_result=None`, `round_index=0`, empty evidence, and the
    immutable identity binding; if the run is cancelled before round 0 retrieves, the
    `cancel_run_checkpoint` CAS flips it to `aborted` and the cooperative check (below)
    finalizes a conservative refusal with zero retrieval/judge/model calls. If the run
    completes naturally, the existing per-round `_save_checkpoint` overwrites this
    minimal row with the full state (same identity binding → UPDATE, not conflict).
  - Cooperative cancellation: at deterministic boundaries (before EACH retrieval — this
    now includes the round-0 `run_fast_path` via the pre-round-0 checkpoint check — and
    before judge / synthesis per round) the loop checks the persisted `aborted` status
    and stops. An already-entered synchronous call is allowed to finish (no hard
    preemption); no NEW Retriever / Judge / Model call is issued after cancel is observed.
  - `aborted` survives restart (it is a persisted row status) and `aborted` checkpoints
    are NOT resumable (E-023 already refuses `aborted` in `resume_run`).
- **C. Backup / Restore** (local MVP, no cloud / no Postgres / no Qdrant Server):
  - **Backup sources of truth = BOTH control-plane SQLite files** (P1-1): the Metadata
    DB (`metadata.db`) AND the Evidence Snapshot Store (`evidence.db`). Qdrant collection(s)
    and the Parent Store remain **rebuildable data planes** (the Reconciler / re-ingest
    reconstruct them from the Metadata DB); they are NOT part of the backup artifact.
  - Backup = consistent SQLite snapshot of EACH control-plane file (SQLite backup API or
    equivalent) taken while honoring active writers; never a raw `cp` of a live, open
    DB file.
  - Backup artifact carries a **versioned manifest** (format version, per-file
    {path, sha256, migration list, timestamp}, corpus/collection pointers). The manifest
    lists exactly the files captured so restore can verify each one.
  - **Restore**: verify manifest/checksum/format/compatibility for EVERY listed file
    FIRST; on failure, leave the current runnable DBs untouched. Before replacing, take
    a pre-restore backup of each target file (or require an explicit offline atomic swap).
    After restore: apply migrations, restore registry pointer, run the `Reconciler` to
    rebuild/reconcile the Qdrant + Parent Store data planes, then verify readiness.
  - Backup / restore logs MUST NOT print Evidence bodies, ACL member lists, or sensitive
    paths. Local backup is explicitly **unencrypted** (no approved encryption dependency).
- **D. Runbooks + Release Reference Profile** (`docs/operations.md`,
  `docs/runbooks/*.md`): readiness-failure, cancel-stuck-run, backup, restore,
  reconcile, index-rollback. Each runbook gives preconditions, dry-run/check command,
  execution steps, success signal, failure rollback, sensitivity notes, and the
  local-MVP-vs-M9 boundary.

### Deferred to sibling / later issues (do NOT pre-build)

- **E-025 → E-027** — formal evaluation / red-team / Research MVP release gate. E-024
  does NOT add evaluation harness logic, Golden Set, or CI gating beyond what the
  runbooks describe.
- **M9** — real Postgres, Qdrant Server, SSO, external connectors, online monitoring,
  canary, distributed/cross-node cancellation. E-024 stays on local SQLite +
  in-process/local Qdrant; cancellation is single-instance and persistent, not
  cross-node.
- Distributed task scheduler, cross-node resume, online backup service, cloud object
  storage, Kubernetes deployment manifests — all out of scope.

### Forbidden / non-goals

- **No LLM / NLP in readiness or backup/restore.** Both are deterministic and hermetic.
- **No new model download / external API in tests** — use the existing `fake` encoders,
  `FakeModel`/`_DevSynthesisModel`, and local Qdrant; tests must be fully hermetic.
- **No "reserved interface" for E-025–E-027 or M9** — do not add unused services,
  tables, flags, or runtime branches "for later". Minimal type boundaries only, and
  only if exercised by a current test.
- **No change to the Planner core** (`planner/`, `executor.py`, `result.py`,
  `budget.py`, `tool_registry.py`) — frozen and out of scope.
- **Cancellation truth = persisted `run_checkpoints` only.** A pure in-memory
  cancellation token must never be the authority (build plan §3623 / E-023 invariant 5).
- **Readiness must never perform a business-side repair or call the LLM** — it is a
  diagnostic gate; the `Reconciler` repairs.
- **Backup/restore must never resurrect deleted/purged evidence** and must fail closed:
  a corrupt/tampered/unsupported backup is rejected, never partially applied.

### Hard invariants (frozen)

1. **Metadata DB is the source of truth** for both checkpoints (E-023) and cancellation.
   An `aborted` decision is persisted, never merely held in memory.
2. **Fail closed on auth.** Foreign-principal / stale-policy / non-discoverable-corpus
   cancellation is refused and is indistinguishable from "run not found" to the client.
3. **Complete/cancel are race-safe.** A DB transaction or CAS prevents a run from being
   marked both `completed` and `aborted`; the persisted state is always a legal
   terminal/running value.
4. **Cooperative, not pre-emptive.** After `cancel` is observed, no NEW Retriever/Judge/
   Model call is issued, but an in-flight synchronous call may finish (no hard kill);
   restart still honors `aborted`.
5. **Readiness never leaks.** The client sees only a status code + fixed generic body;
   no path / SQL / corpus name / exception text / tenant id / evidence id.
6. **Readiness never repairs.** It diagnoses; the `Reconciler` repairs. Readiness never
   calls the LLM.
7. **Backup consistency.** A backup is a consistent snapshot (never a mid-write `cp`);
   its manifest + checksum are verifiable; restore rejects a tampered/unsupported
   artifact and leaves the live DB untouched on failure.
8. **Rebuildable data planes.** Qdrant / Parent Store are reconstructed from the
   Metadata DB (ingest / reconciler) after restore — backup/restore never depends on
   copying vector data.

---

## 2. API contracts

### Readiness / liveness
- `GET /health` → `200 {"status":"ok"}` (process alive; no dependency checks). This
  REUSES the existing route currently registered in `api/main.py` (P1-5); it is migrated
  into `api/routes/health.py` and the legacy inline definition in `main.py` is removed
  (no duplicate `/health` registration; the existing `tests/...` legacy `/health`
  characterization test must still pass).
- `GET /ready` → `200 {"status":"ready"}` when all checks pass; `503 {"status":"unavailable"}`
  on ANY check failure. Body is fixed + generic; the HTTP status is the only signal.
  Registered via the same `api/routes/health.py` module (so a single router owns the
  health surface and double-registration is impossible).
- Check order (each wrapped in a timeout, any exception → 503):
  1. migrations applied (`schema_migrations` has the expected max version) on the
     Metadata DB;
  2. Metadata DB reachable (a trivial read inside the timeout);
  3. Evidence Snapshot Store reachable (a trivial read on `evidence.db`) — it is a
     backup source of truth and must be intact for replay/audit (P1-1);
  4. Corpus Registry non-empty / loadable;
  5. **every** `enabled` + `searchable` corpus whose `active_collection` pointer is set
     MUST resolve to an actually-existing Qdrant collection (P1-6 — NOT "at least one";
     a missing collection for a discoverable corpus fails readiness). An **empty** DB
     with no enabled/searchable corpora registered returns `ready` (the default container
     creates collections lazily on ingest, so a fresh instance must not be marked
     unavailable solely for having zero collections yet). The exact behavior is pinned by
     a release reference profile constant (the set of corpora that MUST have a collection
     for the instance to be `ready`); for the Internal MVP that set = all currently
     registered `enabled`+`searchable` corpora.
  6. required local dependencies (encoders/model) importable.

### Cancellation API (P1-4 — FROZEN, not optional)
- **Dedicated endpoint:** `POST /v1/runs/{run_id}/cancel`. Request body is EMPTY;
  identity is injected from trusted headers (same gateway injection as `/v1/chat`),
  never the body. `run_id` is a path parameter, so no meaningless `query`/`corpus_id`
  is forced onto a cancel request (this also avoids schema-confused cancel/resume/chat
  state combinations on `POST /v1/chat`).
- The route calls `service.cancel_run(run_id, ctx)`, which delegates to the atomic
  `metadata_store.cancel_run_checkpoint(run_id, tenant_id, user_id, session_id,
  policy_version)`.
- **State table (frozen):**
  - `running` + cancel → `aborted` (200).
  - `aborted` + cancel → `aborted` (200, idempotent).
  - `completed` + cancel → `completed` (200; cancellation of an already-finished run is a
    no-op and is NOT an error — fixed to 200, not 409, to keep the client contract simple
    and idempotent).
  - missing / foreign-principal / stale-`policy_version` / undiscoverable-corpus → **same
    generic 404** (reason never leaks).
- The iteration loop consults `metadata_store.load_run_checkpoint(run_id).status`
  (cooperative boundary) before each retrieval (including the round-0 `run_fast_path` via
  the pre-round-0 checkpoint, P1-2) / judge / synthesis step and raises a typed
  `_CancelRequested` control flow that finalizes a conservative refusal WITHOUT calling
  the model.

### Backup / restore (CLI, not HTTP)
- `python -m agentic_rag_enterprise.operations.backup --metadata-db <path>
  --evidence-db <path> --out <dir>` → for EACH control-plane DB, writes a consistent
  snapshot (`<dir>/metadata-<ts>.db`, `<dir>/evidence-<ts>.db`) + `<dir>/manifest.json`
  (format version, per-file {path, sha256, migration list, timestamp}, corpus/collection
  pointers).
- `python -m agentic_rag_enterprise.operations.restore --in <dir> --metadata-db <target>
  --evidence-db <target-evidence>` → verifies manifest/checksum/format for EVERY listed
  file; refuses on any mismatch; takes a pre-restore backup of each target; swaps
  atomically (or requires offline swap); applies migrations; runs the `Reconciler`;
  verifies readiness. On any verification failure, leaves the live DBs untouched.

---

## 3. Failure semantics

- readiness dependency fault / missing migration / missing collection → **503**, generic
  body only.
- cancel by foreign principal / missing run → **4xx**, generic body; reason never leaks.
- cancel idempotent: re-cancel of `aborted` / `completed` returns the same safe result.
- cancel race with complete: CAS/transaction ensures exactly one terminal status wins;
  the loser is a no-op or a safe conflict (never an illegal dual status).
- backup corruption / checksum mismatch / unsupported format → restore refuses, current
  DB preserved, error logged internally (no sensitive detail to client/console beyond
  "backup rejected").
- No dependency fault may be relabelled as a no-evidence answer or a refused run (build
  plan §5.4). A readiness/cancel failure is an operational signal, not a grounding
  outcome.

---

## 4. Migration / rollback strategy

- **No new Metadata DB migration required** for E-024: it reuses the E-023
  `run_checkpoints` `status` column (`aborted` already exists and `resume_run` already
  refuses it). The cancellation CAS is a NEW METHOD on `metadata_store.py`
  (`cancel_run_checkpoint`) — P1-3 — implemented inside one `BEGIN IMMEDIATE`
  transaction, never a standalone `UPDATE`:
  - reads the row's current `status` + identity binding;
  - if identity mismatch → raise `CheckpointIdentityConflict` (fail closed);
  - if `status == completed` → return `already_completed` (no flip, idempotent 200);
  - if `status == aborted` → return `already_aborted` (idempotent 200);
  - if `status == running` → flip to `aborted` and return `running_to_aborted`.
  This is the ONLY writer of the `aborted` transition and makes complete/cancel
  race-safe: `mark_run_checkpoint_done` (complete) and `cancel_run_checkpoint` (abort)
  both run under `BEGIN IMMEDIATE`, so exactly one terminal status wins; a concurrent
  complete+abort yields a single legal value, never both.
- Readiness is read-only — no migration. Evidence Snapshot Store uses its own existing
  `apply_migrations` (no new migration needed for backup, since restore re-applies it).
- Backup/restore operates on the existing Metadata DB + Evidence DB files + manifest;
  rollback = restore the pre-restore backup.
- Index rollback remains the E-022 capability; E-024 runbooks *reference* it but do not
  reimplement it.

---

## 5. Acceptance matrix

### Readiness / liveness
- healthy stack → `/ready` 200.
- Metadata DB unavailable → `/ready` 503.
- migration missing / incompatible → `/ready` 503.
- active collection missing → `/ready` 503.
- `/health` returns 200 even when a recoverable dependency is down.
- readiness body never leaks internal identifiers / exception text.

### Cancellation
- same principal can cancel a `running` run → `aborted`.
- **round-0 cancellation:** a client cancels while the round-0 `run_fast_path` Retriever
  is blocking (checkpoint was created pre-round-0 with `first_result=None`); the run
  transitions to `aborted` and finalizes a conservative refusal with ZERO retrieval/judge/
  model calls (integration test with a blocking round-0 Retriever + concurrent cancel).
- foreign principal cannot cancel → refused (generic 404, reason hidden).
- **stale `policy_version` or undiscoverable corpus cannot cancel** → same generic 404.
- **cancel of a `completed` run → 200, no-op (status stays `completed`).**
- cancel is idempotent (re-cancel of `aborted` → 200 `already_aborted`).
- complete/cancel race yields a single, legal terminal status (CAS under `BEGIN IMMEDIATE`).
- after cancel, zero NEW Judge/Model calls (fault service proves it).
- `aborted` state survives restart and is NOT resumable (E-023 `resume_run` refuses).
- `cancel_run_checkpoint` invariant tests: `running_to_aborted` / `already_aborted` /
  `already_completed` / `not_found` / `CheckpointIdentityConflict`.

### Backup / restore
- consistent backup of BOTH the Metadata DB and the Evidence Snapshot Store taken under
  concurrent writes.
- manifest + per-file checksum correct and verifiable.
- tampered backup (any file checksum mismatch) is rejected; live DBs untouched.
- restore to fresh DBs yields identical documents / ACLs / checkpoints / active collection
  pointer / evidence snapshots / evidence audit log.
- restore failure preserves the old DBs.
- after restore, the `Reconciler` can rebuild a missing Qdrant/Parent-Store data plane.
- readiness passes after restore.

### Runbooks
- `docs/operations.md` + `docs/runbooks/{readiness-failure,cancel-stuck-run,backup,restore,reconcile,index-rollback}.md`
  exist and each contains preconditions / check / steps / success / rollback / sensitivity
  notes / MVP-vs-M9 boundary.

---

## 6. Acceptance commands

```bash
# Targeted E-024 suites (created during implementation).
uv run pytest \
  tests/unit/test_health_readiness.py \
  tests/unit/test_cancellation.py \
  tests/unit/test_backup_restore.py \
  tests/integration/test_e024_runtime_hardening.py \
  -q

# Full suite must stay green (baseline: 799 passed / 1 skipped + new E-024 tests).
uv run pytest tests -q

# Quality gates (from build plan §23.7).
uv run ruff check .
uv run ruff format --check .
uv run mypy src/agentic_rag_enterprise
```

---

## 7. Files (implementation landing zones, not created until acceptance)

- `src/agentic_rag_enterprise/api/routes/health.py` (new) — `/health` (migrated from
  `api/main.py`) + `/ready`. `api/main.py` is edited to REMOVE the inline `/health`
  definition and to `include_router` the health router (no double registration).
- `src/agentic_rag_enterprise/api/routes/runs.py` (new) — `POST /v1/runs/{run_id}/cancel`
  (thin, fail-closed; identity from trusted headers).
- `src/agentic_rag_enterprise/api/main.py` (edit) — remove inline `/health`; register
  health + runs routers.
- `src/agentic_rag_enterprise/api/dependencies.py` (edit) — provide `get_security_context`
  reuse for the cancel route.
- `src/agentic_rag_enterprise/storage/metadata_store.py` (edit) — add
  `cancel_run_checkpoint(run_id, tenant_id, user_id, session_id, policy_version)` (the
  atomic CAS method, P1-3) under `BEGIN IMMEDIATE`.
- `src/agentic_rag_enterprise/services/chat_service.py` (edit) — `cancel_run` delegating
  to `cancel_run_checkpoint`; pre-round-0 minimal `running` checkpoint (P1-2); cooperative
  cancellation check before each retrieval (incl. round-0) / judge / synthesis.
- `src/agentic_rag_enterprise/services/container.py` (edit) — wire `EvidenceSnapshotStore`
  into the default container (so its path is known and shared) and pass it to the service
  where needed; expose it for backup.
- `src/agentic_rag_enterprise/operations/backup.py` (new) — multi-file backup/restore CLI
  (metadata.db + evidence.db + manifest).
- `src/agentic_rag_enterprise/api/schemas.py` — no `action` field on `ChatRequest` (cancel
  is a dedicated endpoint, P1-4); `ChatRequest` stays `query`+`corpus_id`+`run_id`+`resume`.
- `docs/operations.md`, `docs/runbooks/*.md` (new).
- `tests/unit/test_health_readiness.py` (incl. legacy `/health` regression +
  `ready`-fails-on-missing-collection / empty-DB-ready), `tests/unit/test_cancellation.py`
  (CAS invariants + completed/stale-policy/undiscoverable cases), `tests/unit/test_backup_restore.py`
  (multi-file manifest/checksum/tamper), `tests/integration/test_e024_runtime_hardening.py`
  (incl. blocking round-0 Retriever + concurrent cancel).
