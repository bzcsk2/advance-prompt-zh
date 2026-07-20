# Issue E-024 — Health/readiness, persistent cancellation, backup/restore + runbooks

**Milestone:** M7 — Runtime hardening (`E-022` → `E-024`)
**Status:** contract open — implementation pending. Acceptance of this doc unlocks, in
order, the readiness/liveness probe (`api/routes/health.py`), the persistent
cancellation surface (built on the E-023 `run_checkpoints` state machine), the
backup/restore tooling (local SQLite snapshot + restore-through-reconciler), and the
operations runbooks under `docs/runbooks/`.
**Baseline:** `1bc627f` (main HEAD; E-023 CLOSED / ACCEPTED at `a1cd282`; M7 current).
**Build plan refs:** Milestone 7 (§3619 / §3621 / §3623 — exit gate: "依赖故障降级和恢复
测试通过；冻结 release reference profile"), §4214 (FastAPI runtime hardening list —
"持久 Checkpoint / Health / readiness / Cancel / Backend failure degradation"), §5081
(M7 / E-024 scope), §23.7 (quality-gate commands), §29.5 (`depends_on` / `in_scope` /
`deferred_to` rules).
**Depends on:** E-008 / E-008.x (MetadataStore = control-plane source of truth +
`document_builds` lease pattern), E-011 (evidence snapshot), E-022 (Reconciler +
index rollback — the rebuild tool E-024 restore leans on), E-023 (`run_checkpoints`
state machine = the ONLY cancellation truth source). Reuses `storage/metadata_store.py`,
`storage/checkpoint_store.py` (`RunCheckpoint`, `CHECKPOINT_RUNNING`/`COMPLETED`/`ABORTED`),
`services/chat_service.py` (`resume_run`, the iteration loop), `corpus/registry.py`,
`ingestion/reconciler.py`. **No new storage engine is introduced** — local SQLite +
in-process/local Qdrant only (Postgres / Qdrant Server are M9).

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
  - `running → aborted` via a new `cancel_run(run_id, ctx)` service method (or the
    `POST /v1/chat` resume/cancel surface). NO in-memory-only cancellation token is
    ever the source of truth.
  - Re-validates `tenant_id` / `user_id` / `session_id` binding before cancelling;
    foreign principal → fail closed (refused, indistinguishable from "not found").
  - Uses a DB transaction / CAS so `complete` and `cancel` cannot race into an illegal
    state. Idempotent: cancelling an already-`aborted` run returns success.
  - Cooperative cancellation: at deterministic boundaries (before retrieval / judge /
    synthesis per round) the loop checks the persisted `aborted` status and stops. An
    already-entered synchronous call is allowed to finish (no hard preemption); no NEW
    Retriever / Judge / Model call is issued after cancel is observed.
  - `aborted` survives restart (it is a persisted row status) and `aborted` checkpoints
    are NOT resumable (E-023 already refuses `aborted` in `resume_run`).
- **C. Backup / Restore** (local MVP, no cloud / no Postgres / no Qdrant Server):
  - **Backup source of truth = Metadata DB** (the SQLite file). Qdrant / Parent Store
    are rebuildable data planes.
  - Backup = consistent SQLite snapshot (SQLite backup API or equivalent) taken while
    honoring active writers; never a raw `cp` of a live, open DB file.
  - Backup artifact carries a **versioned manifest** (format version, schema/migration
    list, checksum of the DB, timestamp, corpus/collection pointers).
  - **Restore**: verify manifest/checksum/format/compatibility FIRST; on failure, leave
    the current runnable DB untouched. Before replacing, take a pre-restore backup (or
    require an explicit offline atomic swap). After restore: apply migrations, restore
    registry pointer, run the `Reconciler` to rebuild/reconcile the data planes, then
    verify readiness.
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
- `GET /health` → `200 {"status":"ok"}` (process alive; no dependency checks).
- `GET /ready` → `200 {"status":"ready"}` when all checks pass; `503 {"status":"unavailable"}`
  on ANY check failure. Body is fixed + generic; the HTTP status is the only signal.
- Check order (each wrapped in a timeout, any exception → 503):
  1. migrations applied (`schema_migrations` has the expected max version);
  2. Metadata DB reachable (a trivial read inside the timeout);
  3. Corpus Registry non-empty / loadable;
  4. at least one active collection present (per registry pointer);
  5. required local dependencies (encoders/model) importable.

### Cancellation
- Surface option 1 (recommended): `POST /v1/chat` with `resume=true`, `run_id=<id>`, and
  a new `action="cancel"` (defaults to `"resume"`). The route calls
  `service.cancel_run(run_id, ctx)` when `action=="cancel"`.
- `cancel_run` returns a fixed generic success (200) when the run is now `aborted` or
  already `aborted`; refuses (4xx fixed generic) on foreign principal / missing run
  (indistinguishable from not-found). Never returns tenant/run internals.
- The iteration loop consults `metadata_store.load_run_checkpoint(run_id).status`
  (cooperative boundary) before each retrieval / judge / synthesis step and raises a
  typed `_CancelRequested` control flow that finalizes a conservative refusal WITHOUT
  calling the model.

### Backup / restore (CLI, not HTTP)
- `python -m agentic_rag_enterprise.operations.backup --db <path> --out <dir>` → writes
  `<dir>/metadata-<ts>.db` + `<dir>/manifest.json` (format version, migrations, sha256
  of the DB, timestamp, collection pointers).
- `python -m agentic_rag_enterprise.operations.restore --in <dir> --db <target>` →
  verifies manifest/checksum/format; refuses on mismatch; takes a pre-restore backup of
  `<target>`; swaps atomically (or requires offline swap); applies migrations; runs the
  `Reconciler`; verifies readiness. On any verification failure, leaves `<target>`
  untouched.

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

- **No new Metadata DB migration required** for E-024 *if* it reuses the E-023
  `run_checkpoints` `status` column (`aborted` already exists and `resume_run` already
  refuses it). If a migration is needed (e.g. a `cancelled_at` column or a `cancel_run`
  audit table), it follows the E-008.x atomic `BEGIN IMMEDIATE` pattern and is
  idempotently re-applicable.
- Readiness is read-only — no migration.
- Backup/restore operates on the existing Metadata DB file + manifest; rollback = restore
  the pre-restore backup.
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
- foreign principal cannot cancel → refused (4xx, reason hidden).
- cancel is idempotent (re-cancel safe).
- complete/cancel race yields a single, legal terminal status.
- after cancel, zero NEW Judge/Model calls (fault service proves it).
- `aborted` state survives restart and is NOT resumable (E-023 `resume_run` refuses).

### Backup / restore
- consistent backup taken under concurrent writes.
- manifest + checksum correct and verifiable.
- tampered backup is rejected; live DB untouched.
- restore to a fresh DB yields identical documents / ACLs / checkpoints / active
  collection pointer.
- restore failure preserves the old DB.
- after restore, the `Reconciler` can rebuild a missing data plane.
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

- `src/agentic_rag_enterprise/api/routes/health.py` (new) — `/health`, `/ready`.
- `src/agentic_rag_enterprise/services/chat_service.py` — `cancel_run` + cooperative
  cancellation check in the iteration loop.
- `src/agentic_rag_enterprise/operations/backup.py` (new) — backup/restore CLI.
- `src/agentic_rag_enterprise/api/schemas.py` — `action` field on `ChatRequest`.
- `src/agentic_rag_enterprise/api/routes/chat.py` — cancel branch (thin, fail-closed).
- `docs/operations.md`, `docs/runbooks/*.md` (new).
- `tests/unit/test_health_readiness.py`, `tests/unit/test_cancellation.py`,
  `tests/unit/test_backup_restore.py`, `tests/integration/test_e024_runtime_hardening.py`
  (new).
