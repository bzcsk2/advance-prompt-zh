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
**E-024 source design baseline:** `1bc627f` (E-023 CLOSED / ACCEPTED at `a1cd282`; M7 current).
**Contract content SHA:** `c093ad707ec7aac43aa3297c5a546b6e877a70ff` (the R5 remediation
commit). This is the SHA whose content defines the accepted contract; implementation MUST
branch from it and MUST include every R1–R5 clause frozen herein.
**Acceptance marker (this commit):** `c093ad7` is ACCEPTED. The SHA of this acceptance-marker
commit is the **implementation starting HEAD** (reported in the delivery note, not
self-referenced inside the body). Implementation MUST NOT start from the older, FAIL-judged
`8ba1e89`; it starts from the accepted contract SHA `c093ad7` (or this marker HEAD if it
supersedes). E-024 source implementation is now PERMITTED.
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
    Checks (each with a strict timeout; ANY failure → **503** with a fixed, generic body,
    see the frozen readiness rule in §1-E): applied migrations, Metadata DB reachable,
    Evidence Snapshot Store reachable, Corpus Registry loadable, every corpus in the
    release reference profile resolved to a live collection, required local dependencies
    present.
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
    `    `cancel_run_checkpoint` CAS flips it to `aborted` and the cooperative check (below)
    discards the partial result and raises `RunCancelledError` with zero
    retrieval/judge/model calls. If the run
    completes naturally, the existing per-round `_save_checkpoint` overwrites this
    minimal row with the full state (same identity binding → UPDATE, not conflict).
  - Cooperative cancellation: at deterministic boundaries (before EACH retrieval — this
    now includes the round-0 `run_fast_path` via the pre-round-0 checkpoint check — and
    before judge / synthesis per round) the loop checks the persisted `aborted` status
    and stops. An already-entered synchronous call is allowed to finish (no hard
    preemption); no NEW Retriever / Judge / Model call is issued after cancel is observed.
  - **Cancellation is a run-control signal, NOT a grounding outcome (R3-P1-1, FROZEN):**
    when the loop observes `aborted` (whether pre-round-0, mid-loop, or via the
    cooperative check), it DISCARDS the partial result and raises `RunCancelledError`.
    It MUST NOT finalize a `conservative_refusal` / `build_no_evidence_refusal`, because
    `AnswerEnvelope` semantics require `abstained=True` ⇒ no evidence + `stop_reason==
    no_evidence`, which would mislabel a user-initiated cancellation as a "no evidence"
    grounding result (build plan §5.4 — a control signal is not an answer). `RunCancelledError`
    carries no tenant/evidence identifiers and never mutates an `AnswerEnvelope.stop_reason`
    to `no_evidence`. The in-flight Scenario-B Retriever (if any) result is discarded; its
    single execution is still recorded in the internal trace / `tool_calls` counter, but
    that count is NEVER surfaced as a grounding `no_evidence` metric.
  - `aborted` survives restart (it is a persisted row status) and `aborted` checkpoints
    are NOT resumable (E-023 already refuses `aborted` in `resume_run`).
  - **Pre-round-0 recovery state machine (R2-P1-1, FROZEN) — cancellation semantics unified
    (R4-P1-1):** `resume_run` MUST branch on the persisted `status` **FIRST**, and only
    NARROW `first_result` requirements for states that need finalization. `aborted` is a
    run-control signal, so ANY `aborted` checkpoint (regardless of `first_result`) raises
    **`RunCancelledError`** (NOT `ResumeAuthError`) and the API maps it to **HTTP 409
    `{"detail":"run cancelled"}`** — same as the live-loop cancel path. `ResumeAuthError` is
    reserved ONLY for authorization/corruption failures (missing / principal mismatch /
    stale `policy_version` / undiscoverable corpus / corrupt `completed`). The pre-round-0
    checkpoint (`first_result=None`, `round_index=0`, empty evidence) introduces three
    additional (status × first_result) combinations. The complete, exhaustive table:
    - `aborted` + `first_result=None` → raise `RunCancelledError` (NEVER `AssertionError`,
      NEVER `ResumeAuthError("checkpoint_aborted")`, NEVER a `conservative_refusal`); the
      `first_result` being `None` is irrelevant once status is `aborted`.
    - `running` + `first_result=None` → after the standard identity / `policy_version` /
      corpus-discoverability re-auth, **restart from round 0** (re-run `run_fast_path`,
      then continue the normal loop). This is the "crashed before round-0 retrieval"
      recovery. No `AssertionError` is allowed.
    - `completed` + `first_result=None` → **corrupt / illegal state** (a completed run must
      always have a `first_result`). Fail closed: `ResumeAuthError` (or treat as broken,
      like `load_run_checkpoint`'s corruption guard) — do NOT enter Retriever / Judge /
      Model.
    - `aborted` + `first_result` set → raise `RunCancelledError` (same as the
      `first_result=None` aborted case; unified for both).
    - `running` + `first_result` set → normal resume from `round_index` (E-023 behavior).
    - `completed` + `first_result` set → idempotent finalize (E-023 behavior).
    The acceptance matrix MUST cover the first three (pre-round-0) combinations with
    explicit tests: `aborted` (both `first_result` variants) raises `RunCancelledError` →
    HTTP 409; `running` re-runs round 0; `completed+None` raises `ResumeAuthError` — WITHOUT
    ever hitting an `AssertionError`.
- **C. Backup / Restore** (local MVP, no cloud / no Postgres / no Qdrant Server):
  - **Backup sources of truth = BOTH control-plane SQLite files** (P1-1): the Metadata
    DB (`metadata.db`) AND the Evidence Snapshot Store (`evidence.db`). Qdrant collection(s)
    and the Parent Store remain **rebuildable data planes** (the Reconciler / re-ingest
    reconstruct them from the Metadata DB); they are NOT part of the backup artifact.
  - Backup = consistent SQLite snapshot of EACH control-plane file (SQLite backup API or
    equivalent) taken while honoring active writers; never a raw `cp` of a live, open
    DB file.
  - **Cross-file consistency barrier (R2-P1-4, FROZEN):** the two control-plane DBs are
    snapshotted under a single coordinated barrier so the backup reflects one logical
    epoch, NOT two independently-timed consistent files. The `BackupCoordinator`:
    1. acquires a global write barrier on BOTH `MetadataStore` and `EvidenceSnapshotStore`
       (e.g. a briefly-held exclusive lock / `BEGIN IMMEDIATE` on each, or an explicit
       offline/quiesced backup for the Internal MVP — the contract permits the quiesced
       mode);
    2. records a single `snapshot_epoch` (monotonic timestamp/seq) shared by both files;
    3. performs the SQLite backup of each file;
    4. computes per-file sha256 and writes the manifest;
    5. releases the barrier.
    Under concurrent retrieval/audit writes, the barrier guarantees metadata.db and
    evidence.db are at the same `snapshot_epoch` (not metadata@T2 + evidence@T1).
  - **Evidence schema version (R2-P1-4, FROZEN choice):** the Evidence Snapshot Store
    currently creates its schema with inline `CREATE TABLE IF NOT EXISTS` and has no
    `schema_migrations` table. The contract FREEZES the **module-level
    `EVIDENCE_SCHEMA_VERSION` constant** written into the manifest (the other two options —
    an `evidence_schema_version` table, or a DDL fingerprint — are explicitly NOT chosen, to
    avoid manifest/test-interface divergence at implementation time). The manifest's per-file
    entry for the Evidence DB MUST carry this `EVIDENCE_SCHEMA_VERSION` (not a "migration
    list" that does not exist). `storage/evidence_store.py` is added to the allowed-paths
    list so it can expose a safe backup lock, the `EVIDENCE_SCHEMA_VERSION`, and a backup
    primitive.
  - Backup artifact carries a **versioned manifest** (format version, `snapshot_epoch`,
    per-file {path, sha256, evidence_schema_version, timestamp}, corpus/collection
    pointers). The manifest lists exactly the files captured so restore can verify each one.
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
- **Frozen readiness rule (R2-P1-5 — single, non-contradictory):** there is exactly
  ONE readiness decision, stated here and nowhere else in this contract. Delete any
  other phrasing ("at least one active collection", "registry must be non-empty",
  "whose pointer is set").
  1. Corpus Registry read FAILS (exception) → **503**.
  2. Corpus Registry read SUCCEEDS and the **release reference profile is empty** (no
     `enabled`+`searchable` corpus required) → **ready** (200). This is the only path by
     which an "empty" instance is `ready`; it is driven by the profile being empty, NOT
     by "registry empty" or "zero collections".
  3. For **EACH** corpus in the release reference profile (the set of `enabled`+
     `searchable` corpora that MUST be live for this instance; for the Internal MVP that
     set = all currently registered `enabled`+`searchable` corpora), BOTH must hold or
     the instance is **unavailable** (503):
     - the corpus's `active_collection` pointer is NON-EMPTY (a missing pointer is itself
       a failure, not a skip); AND
     - the pointer resolves to an **actually-existing** Qdrant collection.
- Check order (each wrapped in a timeout, any exception → 503):
  1. migrations applied (`schema_migrations` has the expected max version) on the
     Metadata DB;
  2. Metadata DB reachable (a trivial read inside the timeout);
  3. Evidence Snapshot Store reachable (a trivial read on `evidence.db`) — it is a
     backup source of truth and must be intact for replay/audit (P1-1);
  4. Corpus Registry loadable (read success/failure per rule step 1 above);
  5. every corpus in the release reference profile has a non-empty `active_collection`
     pointer resolving to an existing Qdrant collection (rule step 3);
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
  the pre-round-0 checkpoint, P1-2) / judge / synthesis step. When it observes `aborted`
  it raises `RunCancelledError` (R3-P1-1) — it does NOT call the model and does NOT return
  an `AnswerEnvelope`. The `POST /v1/chat` (and `resume` path) handler catches
  `RunCancelledError` and maps it to a **fixed HTTP 409** with a fixed generic body
  `{"detail":"run cancelled"}`, returning NO `AnswerEnvelope` and recording NO
  `no_evidence` / `abstained` grounding outcome. The dedicated `POST /v1/runs/{run_id}/cancel`
  endpoint still returns **200** (it is the cancel *command*, not the cancelled run's
  result). Both Scenario A and Scenario B produce the same cancel HTTP semantics (409 on
  the chat/resume request); the only difference is the internal trace, which in Scenario B
  records the 1 already-executed (discarded) Retriever call.

### Backup / restore (CLI, not HTTP)
- Unified command: `python -m agentic_rag_enterprise.operations.backup
  {backup|restore}` (subcommand form; `operations/__init__.py` + `operations/backup.py`
  + `operations/restore.py` are the landing zones). The CLI operates ONLY on the
  generation-pointer layout (R4-P1-2): production storage is NEVER two standalone absolute
  files opened directly. The config interface is:
  - `settings.storage_generations_root` (dir holding `restore-generations/<generation-id>/`).
  - `settings.current_generation_pointer` (`CURRENT` pointer file under that root).
  - `metadata_db_path` / `evidence_db_path` are ONLY the **internal filenames inside a
    generation** (i.e. `metadata.db` / `evidence.db` within
    `restore-generations/<id>/`), OR an explicit hermetic **non-managed mode** used solely
    by unit tests that pass both as absolute paths to bypass generation management. They
    are NOT production direct-open absolute paths.
- `... backup --out <dir>` reads the live DB pair via `CURRENT`, and is frozen below.
- `... restore --in <backup-dir> --generations-root <root>` — restore takes NO
  `--metadata-db` / `--evidence-db` target-file arguments. It stages a new generation under
  `<root>/restore-generations/<new-id>/`, verifies, and flips `CURRENT`. The old target-file
  CLI form is removed.
- `... backup --out <dir>` → the `BackupCoordinator` acquires the cross-file write
  barrier, records `snapshot_epoch`, snapshots BOTH control-plane DBs
  (`<dir>/metadata-<ts>.db`, `<dir>/evidence-<ts>.db`), computes per-file sha256 + the
  Evidence schema version (`EVIDENCE_SCHEMA_VERSION`) and writes `<dir>/manifest.json` (format version,
  `snapshot_epoch`, per-file {path, sha256, evidence_schema_version, timestamp},
  corpus/collection pointers). Then releases the barrier.
- `... restore --in <dir>` →
  verifies manifest/checksum/`snapshot_epoch`/format for EVERY listed file (incl. the
  Evidence schema version `EVIDENCE_SCHEMA_VERSION`); refuses on any mismatch. **Crash-atomic
  restore is mandatory (R3-P1-2, FROZEN): restores are NOT in-place file overwrites.**
  - **Generation-pointer layout (R4-P1-2):** a single `CURRENT` pointer file (at
    `settings.current_generation_pointer`) names a generation directory under
    `settings.storage_generations_root/restore-generations/<generation-id>/`, which contains
    exactly `{metadata.db, evidence.db, manifest.json}`. Services read `metadata.db` +
    `evidence.db` ONLY through the path that `CURRENT` names; they never open a raw DB path
    directly.
  - **Restore procedure:** verify the two new DBs in a freshly-written staging generation
    directory → `fsync` both the files AND their parent directories. Then perform the pointer
    commit (R4-P1-4, FROZEN durability sequence):
    1. write `CURRENT.tmp` (same directory as `CURRENT`) containing the new `<generation-id>`;
    2. `fsync(CURRENT.tmp)`;
    3. `os.replace(CURRENT.tmp, CURRENT)` (atomic rename);
    4. `fsync(parent_directory_of_CURRENT)`.
    The durable commit point is step 4 (parent-dir `fsync`), NOT the `os.replace` call return.
    Only after step 4 is the new generation defined as durable. Because only ONE pointer flip
    changes which DB pair the process uses, the two DBs can never be observed at different
    generations by a restarted process. The old generation directory is retained (for
    rollback) but is not pointed-at.
  - **Offline restore ordering (R4-P1-3 / R5-P1-1, FROZEN = option A, offline):** restore is
    an OFFLINE operation for the local Internal MVP. The pointer flip MUST happen while NO
    serving/worker process holds either SQLite DB, i.e. the service is stopped FIRST. The
    mandatory operator sequence (runbook + code MUST follow this exact order):
    1. stop the service;
    2. verify no serving/worker process holds either SQLite DB (no open handles);
    3. take a pre-restore backup of the currently pointed generation;
    4. stage and validate the new generation (manifest/checksum/`EVIDENCE_SCHEMA_VERSION`);
    5. run the staged-generation reconciler + readiness verification;
    6. durably flip `CURRENT` (the 4-step sequence, commit point = parent-dir fsync);
    7. restart the service;
    8. verify `/ready`.
    The SQLite Store opens connections at container startup, so an already-running process
    does NOT auto-switch — online reload is explicitly OUT of scope. Under NO circumstance
    may restore perform `flip CURRENT → stop service`; flipping while the service is live
    would let accepted writes to the old generation vanish on restart. A post-restore restart
    is therefore mandatory and happens AFTER the flip.
  - **Bootstrap / `CURRENT` lifecycle (R4-P1-3 / R5-P1-2, FROZEN startup correctness):** the
    instance first decides fresh vs legacy vs fail-closed by a MUTUALLY-EXCLUSIVE check (it
    MUST NOT create an empty `gen-0` whenever a generation directory already exists):
    - **TRUE FRESH:** no `CURRENT`, no `CURRENT.tmp`, generations root does not exist OR is
      empty, AND no legacy `metadata.db` / `evidence.db` → create generation 0
      (`restore-generations/gen-0/{metadata.db, evidence.db, manifest.json}`) with applied
      migrations + `EVIDENCE_SCHEMA_VERSION`, then write `CURRENT` → `gen-0` (4-step durable
      sequence). No legacy raw-DB mode.
    - **LEGACY MIGRATION:** no `CURRENT`, generations root empty, AND BOTH legacy
      `metadata.db` + `evidence.db` present (as a pair) → one-time migration MOVES them into
      `restore-generations/gen-0/`, writes `CURRENT` → `gen-0`, renames the originals. A
      single present legacy file (missing sibling) → FAILS CLOSED (`RuntimeError`), repair
      from backup.
    - **FAIL CLOSED (R5-P1-2):** no `CURRENT` BUT the generations root already contains ANY
      generation directory (e.g. `gen-17/` with complete history while `CURRENT` was
      accidentally deleted) → FAILS CLOSED (`RuntimeError`). The instance MUST NOT silently
      create an empty `gen-0` and bypass the existing data; the operator must restore
      `CURRENT` from backup.
    - no `CURRENT` but only `CURRENT.tmp` present (interrupted flip) → FAILS CLOSED
      (`RuntimeError`); do NOT bootstrap, do NOT serve — recover by completing/aborting the
      flip from the staged generation.
    - `CURRENT` empty/invalid, OR points at a non-existent generation directory, OR the
      pointed-at generation fails manifest/checksum/`EVIDENCE_SCHEMA_VERSION` validation →
      FAILS CLOSED (`RuntimeError`), refusing to serve; operator restores from backup.
  - Crash windows (R4-P1-4, FROZEN crash-point definitions):
    - before the `os.replace` (steps 1–2) → `CURRENT` still names the old generation;
      restart loads the FULL OLD generation.
    - after `os.replace` but before the parent-dir `fsync` (step 3 done, step 4 pending) →
      on power loss the system MAY load old OR new; startup MUST accept EITHER and validate
      the full pointed-at generation (manifest + both files) before serving — and if the
      pointed-at generation is invalid it FAILS CLOSED (never serves a half-written pair).
    - after parent-dir `fsync` (step 4 done) → restart loads the FULL NEW generation.
    There is **no** window where `CURRENT` points at a half-swapped pair, because the swap is
    a single atomic pointer rename + dir fsync, not two sequential DB-file replacements.
  - Before flipping `CURRENT`, the restore also takes a pre-restore backup of the currently
    pointed-at generation (for rollback), runs the `Reconciler`, and verifies readiness on the
    staged generation. On any verification failure, the `CURRENT` pointer is left unchanged and
    the live DBs are untouched.
- `restore-generations/` layout + `CURRENT` pointer, the `BackupCoordinator` staging-verify-
  fsync-then-rename protocol, and the startup resolver (`CURRENT`→load generation, with the
  bootstrap/fail-closed rules above) are part of the E-024 implementation landing zones
  (`operations/backup.py`, `operations/restore.py`;
  `config.py` gains the generations root + `CURRENT` resolution). The manifest must carry the
  `generation_id` and both `metadata.db` + `evidence.db` fingerprints (`EVIDENCE_SCHEMA_VERSION` + per-file sha256) so a restart can validate that `CURRENT`'s named generation is internally consistent.

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
  refuses it). **Both** terminal transitions become transactional CAS methods on
  `metadata_store.py`, each inside one `BEGIN IMMEDIATE` transaction (R2-P1-3 — the race
  is bidirectional; freezing only the cancel side is insufficient):
  - `cancel_run_checkpoint(run_id, tenant_id, user_id, session_id, policy_version)`:
    - reads the row's current `status` + identity binding;
    - identity mismatch → raise `CheckpointIdentityConflict` (fail closed);
    - `completed` → return `already_completed` (no flip, idempotent 200);
    - `aborted` → return `already_aborted` (idempotent 200);
    - `running` → flip to `aborted` via `UPDATE ... SET status='aborted' WHERE run_id=?
      AND status='running'` and return `running_to_aborted`.
  - `complete_run_checkpoint(run_id)` (REPLACES the current unconditional
    `mark_run_checkpoint_done` UPDATE — the old method is removed/renamed so no code path
    can flip `aborted → completed`):
    - `running` → `completed` via `UPDATE ... SET status='completed' WHERE run_id=? AND
      status='running'` (return `running_to_completed`);
    - `completed` → `already_completed`;
    - `aborted` → `already_aborted` (a cancelled run is NOT silently completed);
    - missing → `not_found`.
  Because BOTH use `WHERE status='running'` guards inside `BEGIN IMMEDIATE`, the
  interleaving `cancel → aborted` then stale `complete` is a no-op (the `WHERE` matches
  nothing), and `complete → completed` then stale `cancel` is likewise a no-op. Exactly
  one terminal status survives; never both. **ALL** iteration terminal paths
  (`answer_with_iteration` end-of-loop, `_BreakLoop` handlers, resume terminal paths)
  MUST call `complete_run_checkpoint`, never a raw `UPDATE`.
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
- **Round-0 cancellation — TWO distinct scenarios (R2-P1-2, MUST be split):**
  - **Scenario A (cancel BEFORE the round-0 pre-call check):** the run is cancelled before
    the loop's cooperative check fires (pre-round-0 checkpoint already `aborted`). On
    resume/continue the loop issues **0 Retriever / 0 Judge / 0 Model** calls and raises
    `RunCancelledError` (`tool_calls=0`, no `AnswerEnvelope`, no `no_evidence` metric).
  - **Scenario B (cancel AFTER the round-0 Retriever is already entered & blocking):** the
    in-flight synchronous Retriever call is allowed to RETURN (cooperative cancellation
    never hard-kills an entered call); its result is **discarded**; the loop then issues
    **0 additional Retriever / 0 Judge / 0 Model** calls, keeps `status=aborted`, and raises
    `RunCancelledError`. The internal trace / `tool_calls` counter MUST truthfully record the
    1 already-executed retrieval (NOT 0) — the count reflects reality, but it is NEVER
    surfaced as a grounding `no_evidence` outcome.
  Both scenarios are covered by integration tests (blocking round-0 Retriever + concurrent
  cancel, plus a cancel-before-check variant). **Both yield the same HTTP cancel semantics:
  `POST /v1/chat` (and the resume path) return HTTP 409 `{"detail":"run cancelled"}` with NO
  `AnswerEnvelope`; the dedicated cancel endpoint still returns 200.**
- **Cancellation is a run-control signal, never a grounding outcome (R3-P1-1, MUST test):**
  - `RunCancelledError` is raised on observed `aborted` (pre-round-0, mid-loop, cooperative
    check) and is NOT a `conservative_refusal` / `build_no_evidence_refusal`.
  - `POST /v1/chat` catches `RunCancelledError` → fixed **HTTP 409** + fixed body
    `{"detail":"run cancelled"}`; returns NO `AnswerEnvelope`; records NO `no_evidence` /
    `abstained` grounding outcome.
  - the cancellation MUST NOT set `AnswerEnvelope.stop_reason = "no_evidence"` and MUST NOT
    increment any no-evidence metric; a coverage/judge fault that would have produced
    `no_evidence` is a DIFFERENT (grounding) path and is unaffected.
  - `RunCancelledError` carries no tenant / evidence identifiers (no leak).
- foreign principal cannot cancel → refused (generic 404, reason hidden).
- **stale `policy_version` or undiscoverable corpus cannot cancel** → same generic 404.
- **cancel of a `completed` run → 200, no-op (status stays `completed`).**
- cancel is idempotent (re-cancel of `aborted` → 200 `already_aborted`).
- complete/cancel race yields a single, legal terminal status (bidirectional CAS under
  `BEGIN IMMEDIATE`, see §4).
- after cancel, **zero NEW** Judge/Model calls (fault service proves it); an already-
  entered round-0 Retriever may return exactly once and is discarded (Scenario B).
- `aborted` state survives restart and is NOT resumable (E-023 `resume_run` refuses).
- `cancel_run_checkpoint` invariant tests: `running_to_aborted` / `already_aborted` /
  `already_completed` / `not_found` / `CheckpointIdentityConflict`.
- **Bidirectional complete/cancel race tests (R2-P1-3):** using the SQL `WHERE
  status='running'` guards under `BEGIN IMMEDIATE`:
  - cancel acquires the txn first → `aborted`; a subsequent stale `complete_run_checkpoint`
    is a no-op (status stays `aborted`);
  - complete acquires the txn first → `completed`; a subsequent stale
    `cancel_run_checkpoint` is a no-op (status stays `completed`);
  - cancel commits, then OLD execution flow tries complete → no-op (`already_aborted`);
  - complete commits, then client tries cancel → no-op (`already_completed`).
  In every case the final status is a single legal value, never both `aborted` and
  `completed`.

### Backup / restore
- consistent backup of BOTH the Metadata DB and the Evidence Snapshot Store taken under
  concurrent retrieval/audit writes, coordinated by the `BackupCoordinator` barrier so
  both files share one `snapshot_epoch` (NOT independently-timed consistent files).
- manifest carries per-file sha256 + the `EVIDENCE_SCHEMA_VERSION` constant + `generation_id`;
  both DBs verifiable.
- tampered backup (any file checksum or `EVIDENCE_SCHEMA_VERSION` mismatch) is rejected; live
  DBs untouched.
- restore uses the **generation-pointer** crash-atomic protocol (R3-P1-2): staging
  generation dir + `fsync` files & dirs + atomic `CURRENT` rename; services load DBs only via
  `CURRENT`. A sequential two-file in-place replace is NOT permitted.
- restore to fresh DBs yields identical documents / ACLs / checkpoints / active collection
  pointer / evidence snapshots / evidence audit log, all at the same `snapshot_epoch`.
- restore failure preserves the old DBs (live `CURRENT` unchanged) and is testable.
- after restore, the `Reconciler` can rebuild a missing Qdrant/Parent-Store data plane.
- readiness passes after restore (on the staged generation, before pointer flip).
- **Crash-atomicity crash-points (R4-P1-4, MUST test):** simulate host/power loss during the
  4-step durable pointer commit and assert restart resolves to a FULL old generation OR a FULL
  new generation, never a mix:
  - (a) before `os.replace` (staged files + `CURRENT.tmp` written, rename not done) → restart
    loads the original `CURRENT` generation (full old state, unchanged).
  - (b) after `os.replace` but before the parent-dir `fsync` (step 3 done, step 4 pending) →
    restart MAY load old OR new; the test asserts startup ACCEPTS and fully validates the
    pointed-at generation (manifest + both files + `EVIDENCE_SCHEMA_VERSION`); if invalid it
    FAILS CLOSED — never serves a half-written pair.
  - (c) after parent-dir `fsync` (step 4 done, the durable commit point) → restart loads the
    NEW generation (full new state); old generation retained for rollback.
  In every crash-point the two DBs are observed at the SAME generation via `CURRENT`; a
  mixed-generation pair is impossible by construction.
- **`CURRENT` bootstrap / fail-closed (R4-P1-3 / R5-P1-2, MUST test):** TRUE FRESH (no
  `CURRENT`, no `CURRENT.tmp`, empty/nonexistent generations root, no legacy pair) creates
  gen-0 via the durable sequence; LEGACY PAIR migrates into gen-0; but no `CURRENT` with a
  non-empty generations root containing ANY generation (e.g. `gen-17/` present) MUST FAIL
  CLOSED at startup (no silent empty gen-0); single legacy file, `CURRENT` naming a missing/
  invalid generation, or only `CURRENT.tmp` present also FAIL CLOSED at startup.
- **Offline activation ordering (R4-P1-3 / R5-P1-1, MUST test):** restore follows the frozen
  stop → verify-no-open-handles → pre-restore-backup → stage/validate → reconciler/readiness
  → durably flip `CURRENT` → restart → `/ready` sequence; an implementation that flips
  `CURRENT` while the service is still running is rejected. After a successful restore
  (post-flip, post-restart), the restarted service opens the new generation via `CURRENT`; an
  already-running process without restart does NOT switch (online reload out of scope).

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
- `src/agentic_rag_enterprise/storage/metadata_store.py` (edit) — add BOTH transactional
  CAS methods under `BEGIN IMMEDIATE` (R2-P1-3):
  `cancel_run_checkpoint(run_id, tenant_id, user_id, session_id, policy_version)` and
  `complete_run_checkpoint(run_id)` (replaces the unconditional `mark_run_checkpoint_done`
  UPDATE; all iteration terminal paths use it).
- `src/agentic_rag_enterprise/storage/evidence_store.py` (edit, R2-P1-4) — expose a safe
  backup lock / the `EVIDENCE_SCHEMA_VERSION` constant and
  a backup primitive usable by the `BackupCoordinator`.
- `src/agentic_rag_enterprise/services/chat_service.py` (edit) — `cancel_run` +
  `complete_run_checkpoint` delegation; pre-round-0 minimal `running` checkpoint (P1-2);
  `resume_run` FIRST branches on `status` then narrows `first_result` per the R2-P1-1
  table (assert is removed for `first_result=None`); cooperative cancellation check before
  each retrieval (incl. round-0) / judge / synthesis.
- `src/agentic_rag_enterprise/services/container.py` (edit, R4-P1-2) — production storage is
  generation-managed: the container resolves the live DB pair via `CURRENT`
  (`settings.storage_generations_root` / `settings.current_generation_pointer`) and does NOT
  open raw `metadata_db_path`/`evidence_db_path` absolute paths in production. It exposes a
  **non-managed mode** used only by hermetic unit tests: when both
  `metadata_db_path`+`evidence_db_path` are passed as absolute paths (and no generations root
  is set), the container opens them directly and skips `CURRENT` resolution.
  `DefaultServiceContainer.from_settings(settings)` (production) and a test-only constructor
  accepting explicit `(metadata_db_path, evidence_db_path)` for the non-managed mode.
- `src/agentic_rag_enterprise/config.py` (edit, R4-P1-2) — production settings:
  `storage_generations_root: str`, `current_generation_pointer: str`; `metadata_db_path` /
  `evidence_db_path` become internal generation filenames (default `"metadata.db"` /
  `"evidence.db"`) used only inside a generation dir or in the non-managed test mode. CLI/
  backup read `storage_generations_root` + `current_generation_pointer`, NOT direct DB files.
- `.env.example` (edit, R4-P1-2) — document `STORAGE_GENERATIONS_ROOT` /
  `CURRENT_GENERATION_POINTER`; remove standalone `EVIDENCE_DB_PATH` as a production direct-
  open path.
- `src/agentic_rag_enterprise/operations/__init__.py` (new, P2) — package init for the
  `operations` CLI namespace.
- `src/agentic_rag_enterprise/operations/backup.py` (new) — `BackupCoordinator` (cross-file
  barrier + `snapshot_epoch`) + `backup` subcommand (reads live pair via `CURRENT`).
- `src/agentic_rag_enterprise/operations/restore.py` (new, P2) — `restore` subcommand
  (staging + 4-step durable pointer commit + bootstrap/fail-closed rules + pre-restore backup,
  reconciler, readiness; takes `--in` + `--generations-root`, NO target-DB-file args).
- `src/agentic_rag_enterprise/api/schemas.py` — no `action` field on `ChatRequest` (cancel
  is a dedicated endpoint, P1-4); `ChatRequest` stays `query`+`corpus_id`+`run_id`+`resume`.
- `docs/operations.md`, `docs/runbooks/*.md` (new) — the restore runbook MUST state that
  restore is OFFLINE and follows the frozen order: stop service → verify no open SQLite
  handles → pre-restore backup → stage/validate → reconciler/readiness → durably flip
  `CURRENT` → restart service → verify `/ready` (R4-P1-3 option A / R5-P1-1).
- **Test DB hygiene (R4-P1-2):** hermetic unit tests use the container's non-managed mode with
  a PAIR of temp absolute paths; integration tests exercise the generation-pointer layout
  (bootstrap gen-0, legacy-migration, flip, restart) and MUST NOT open a root-dir
  `evidence.db` in production mode.
- `tests/unit/test_health_readiness.py` (incl. legacy `/health` regression,
  frozen-readiness-rule cases: registry-read-fail→503 / empty-profile→ready /
  missing-pointer→503 / missing-collection→503), `tests/unit/test_cancellation.py`
  (CAS invariants + bidirectional race + completed/stale-policy/undiscoverable + pre-
  round-0 state-machine A/B scenarios), `tests/unit/test_backup_restore.py`
  (multi-file manifest/checksum/`snapshot_epoch`/evidence-schema-version/tamper + barrier
  under concurrent writes), `tests/integration/test_e024_runtime_hardening.py`
  (incl. blocking round-0 Retriever + concurrent cancel).
