# AGENTS.md — Agentic RAG Enterprise

## Implementation Spec
`docs/agentic-rag-enterprise-build-plan.md`

## Current Milestone & Issue
- Milestone: **M1** — Secure single-corpus data vertical slice
- Issue: **E-007** — Port parent-child chunking + hybrid retrieval from upstream (algorithm only, enterprise security envelope) — CLOSED at `ccb52dc`.
- Issue: **E-007.1** — Audit-remediation of E-007 (5 P1 + 4 P2 findings) — CLOSED at `b0dbf6f`.
- Issue: **E-008** — Implement idempotent ingestion job and active-version protocol (M1) — CLOSED at `139df74`.
- Issue: **E-008.1** — Audit-remediation of E-008 (7 MUST violations) — CLOSED at this commit; full contract at `docs/issue-e0081-contract.md`.
- Issue: **E-008.2** — Audit-remediation of E-008.1 (real-crash + concurrency windows) — CLOSED at this commit; full contract at `docs/issue-e0082-contract.md`.
- Issue: **E-008.3** — Audit-remediation of E-008.2 (lease-ownership + verify precision) — CLOSED at `b7012cb`; full contract at `docs/issue-e0083-contract.md`.
- Issue: **E-008.4** — Audit-remediation of E-008.3 (state-transition gaps: deprecated-version idempotency, `previous_active_version` transfer across takeover, same-`job_id` concurrency) — CLOSED at this commit; full contract at `docs/issue-e0084-contract.md`.
- Prior issue **E-006.1** — CLOSED at `807aa0c` (deprecated flag in PEP, real cross-tenant tests, Qdrant PDP/PEP equivalence).

> **AGENTS.md is a slim entry point** (build plan §1.7): current Milestone/Issue, fixed
> paths, and standard commands only. Detailed Issue Contracts are versioned docs under
> `docs/` (linked below), not copied here. From E-008 onward, do not inline full contract
> prose into AGENTS.md.

## Fixed Paths
```bash
UPSTREAM_REPO=/vol4/Agent/agentic-rag-for-dummies
TARGET_REPO=/vol4/Agent/agentic-rag-enterprise
```

## Fixed Commits (M1 baseline)
- Target: `3748b33ffa37a0f977d9ba448e6d760a639b5eba` (main)
- Upstream: `8b3e5ff0619f7ede593d728e4a8b459fbbec9b08` (main, tag v2.3)

## Permanent Rules (all milestones)
1. **DO NOT modify upstream** (`/vol4/Agent/agentic-rag-for-dummies/`).
2. Target uses `src/agentic_rag_enterprise/` package layout.
3. `pyproject.toml` is the single source of truth for dependencies.
4. Do not create empty code directories.
5. Keep existing working tree changes; do not reset, checkout, or overwrite.

## E-005 Allowed Changes (M1 only) — completed
- `src/agentic_rag_enterprise/domain/` — create or modify domain models
- `migrations/` — create or modify migration scaffolding
- `tests/test_domain_models.py` — create or modify
- `AGENTS.md` — update
- Do not modify existing modules under `src/agentic_rag_enterprise/{agents,graph,retrieval,api,evals,observability,ingestion,security,config,schemas,providers}`.
- No upstream modifications. No push, no PR creation.

## E-006 Allowed Changes (M1 only)
- `src/agentic_rag_enterprise/security/` — create or modify policy truth table, PEP filter, authorization
- `src/agentic_rag_enterprise/domain/security.py` — may be read; SecurityContext already matches spec §7.5
- `tests/security/` — create authorization tests (truth table, corpus discoverability, PEP filter)
- `AGENTS.md` — update
- Keep `security/policy.py:AccessPolicy.can_access(user_id, corpus)` shim so the M0 baseline
  characterization tests in `tests/baseline/test_retrieval_baseline.py` stay green.
- No upstream modifications. No push, no PR creation.

## E-006.1 Allowed Changes (M1 only) — CLOSED at `807aa0c`
- `src/agentic_rag_enterprise/security/filter.py` — add `deprecated == false` to `build_access_filter`
  and to `resource_passes_filter`; this makes the PEP filter structurally express the active,
  non-deprecated invariant that `migrations/001_initial_schema.sql` intends for rows. (Note: the
  migration does **not** add a runtime DB CHECK — the PEP filter is the enforcement point, not the DDL.)
- `tests/security/test_authorization.py` — replace the fake same-tenant "cross-tenant" rows with
  real cross-tenant cases (ctx tenant != acl tenant), add `deprecated` unit test, bump `must` count.
- `tests/integration/test_qdrant_authorization.py` — new; real in-memory Qdrant collection proving
  PDP (`evaluate_access`) == Qdrant Filter (`build_access_filter`) over the ACL matrix.
- `AGENTS.md` — update issue + record E-007 constraint.
- No upstream modifications. No push, no PR creation.

## E-007 Issue Contract (M1 only) — CLOSED at `ccb52dc`
Port parent-child chunking + hybrid retrieval from upstream (`agentic-rag-for-dummies`, tag v2.3,
read-only). Port **algorithms only**; never upstream trust boundaries.

### Allowed paths
- `src/agentic_rag_enterprise/ingestion/` — port parent-child chunking algorithm
  (heading-aware split, merge-small / split-large parents, rebalance, recursive child split).
  Parent/child IDs MUST be content-addressed + tenant-scoped (`sha256`, NOT filename-derived);
  chunks MUST carry provenance (`document_id`, `tenant_id`, `corpus_id`, `section_path`,
  `document_version`).
- `src/agentic_rag_enterprise/retrieval/` — hybrid retrieval, parent reader (second-auth),
  corpus-discoverability gate.
- `src/agentic_rag_enterprise/storage/` — new Qdrant hybrid vector store + in-memory parent store.
- `src/agentic_rag_enterprise/security/` — may extend (e.g. `can_discover_corpus` /
  `allowed_corpus_ids`); the PEP/PDP truth table stays `build_access_filter` / `evaluate_access`
  / `resource_passes_filter`.
- `tests/{unit,integration,security,fixtures}/` — new tests + shared fixtures.
- `pyproject.toml`, `uv.lock` — dependencies (`langchain-text-splitters`, `fastembed`;
  `qdrant-client` already present).
- `AGENTS.md`, `docs/upstream-capability-map.md`.

### Forbidden
- No upstream modifications. No push, no PR creation.
- MUST use `evaluate_access` / `build_access_filter` / `resource_passes_filter`; MUST NOT use
  `AccessPolicy.can_access` on any retrieval path.
- No filename-derived parent IDs; no filter-less retrieval.
- Do **NOT** add encoders/config to `config.py` / `Settings` — inject them (the E-007 contract
  permits `ingestion/`, `retrieval/`, `storage/`, `security/`, `tests/...`, pyproject only;
  `config.py` and `domain/` are out of scope).
- Do not modify existing modules outside the allowed paths.

### Security requirements
1. **Corpus discoverability gate** — every retrieval entry point MUST validate
   `can_discover_corpus` / `allowed_corpus_ids` (tenant match + enabled + searchable +
   `allowed_corpus_ids`) BEFORE `build_access_filter`, because the filter does not read
   `allowed_corpus_ids`. Fail-closed (`CorpusNotDiscoverableError`).
2. **PEP/PDP are the filter functions** — `build_access_filter` (Qdrant `Filter`) and
   `evaluate_access` (PDP) are authoritative. Empty `allowed_security_levels` fails closed by
   raising `EmptyAuthorizationScopeError` (the PEP mirrors the PDP, which denies on empty levels);
   empty `groups` simply omits the group `should`/`must_not` conditions (also matching the PDP,
   where an empty `set(ctx.groups)` matches no group allow/deny entry). There is **no**
   sentinel-value design — equivalence is preserved structurally.
3. **Parent second authorization** — `ParentReader` is the ONLY authorized parent accessor; it
   re-verifies identity (tenant/corpus/document/version), lifecycle (active, not deprecated),
   ACL-metadata consistency, and `resource_passes_filter`. Fail-closed (`ParentAuthorizationError`).
4. **`SecurityContext` required** on every retrieval path.
5. **M0 baseline regression** (`tests/baseline/test_retrieval_baseline.py`) MUST stay green;
   `SimpleChunker` + mock `Retriever` retained as adapters.

### Acceptance tests
- `tests/unit/test_parent_child_chunker.py`
- `tests/integration/test_qdrant_hybrid_retrieval.py`
- `tests/security/test_parent_reader.py`
- `tests/integration/test_e007_end_to_end.py`
- `tests/baseline/` MUST remain green.
- `ruff`, `mypy src/agentic_rag_enterprise` clean.

## E-007.1 Issue Contract (M1 only) — CLOSED at `b0dbf6f`
Security-audit remediation of E-007 (audit verdict: **Conditional Fail**). Baseline `ccb52dc` is
kept intact; E-007.1 is a narrow fix commit. Scope is a strict subset of the E-007 allowed paths
(`security/`, `retrieval/`, `ingestion/`, `storage/`, `tests/...`, `AGENTS.md`,
`docs/upstream-capability-map.md`, `pyproject.toml`). No upstream modifications.

### P1 fixes (mandatory, all done)
- **P1-1 — PDP/PEP equivalence on empty scopes.** Removed the `_fail_closed` sentinel from
  `security/filter.py`. `build_access_filter` now raises `EmptyAuthorizationScopeError` when
  `allowed_security_levels` is empty (mirrors `evaluate_access`, which denies); empty `groups`
  simply omits the group conditions. Added `tests/integration/test_qdrant_hybrid_retrieval.py`
  cases for reserved-level / reserved-payload injection on empty groups/levels.
- **P1-2 — ParentReader permissive defaults → fail-closed.** `_validate_parent_auth_metadata`
  now rejects a parent whose auth metadata is missing or mis-typed (`status` str, `deprecated`
  bool, `acl_scope` ∈ {tenant,restricted}, ACL lists are `list[str]`). `load_parent_for_hit`
  reads those fields directly (no permissive `.get` defaults). New parametrized cases in
  `tests/security/test_parent_reader.py` (6 missing + 6 malformed → `ParentAuthorizationError`).
- **P1-3 — Rebalance completeness + separator off-by-two.** Ported upstream `_rebalance_pair`
  into `ingestion/chunker.py`; `_clean_small_chunks` now accounts for the `"\n\n"` separator
  (`+2`) and runs a second pass that rebalances any remaining small segment with a neighbor. A
  parent exceeding `max_parent_size` after rebalancing raises `ValueError`. New chunker tests
  cover orphan rebalance and the max-with-separator bound.
- **P1-4 — `document_version` in the ID.** `chunk_markdown` now requires `document_version`
  (no default); `_make_parent_id` folds it into the content-addressed blob, so distinct versions
  get distinct parent/child ids (no cross-version overwrite). New tests assert required-ness and
  version-scoped distinct ids.
- **P1-5 — real ChildChunk → PointStruct mapper in E2E.** Added production
  `child_chunk_to_point(child, acl, *, status, deprecated, dense_encoder, sparse_encoder)` to
  `storage/vector_store.py` (stable `uuid5` point id, full provenance + ACL payload). The E2E test
  `_ingest` now runs the **real** chain: `chunk_markdown(..., document_version="v1")` →
  `child_chunk_to_point` → Qdrant; parents are the chunker's own `ParentChunk` (id/version kept)
  with ACL metadata supplemented only.

### P2 fixes (all included per decision)
- **P2-1 — internalize HybridRetriever.** Renamed to `_HybridSearchAdapter` (private); removed
  from `retrieval/__init__.py` exports. New architecture test
  `tests/unit/test_retrieval_boundary.py` enforces non-export.
- **P2-2 — precise exception capture.** `retriever.py` no longer wraps the parent pass in a bare
  `except Exception:`; only `ParentAuthorizationError` is caught (denials), so storage/programming
  faults propagate.
- **P2-3 — `denied_parent_ids` → `denied_parent_count`.** `RetrievalResult.denied_parent_ids`
  renamed to `denied_parent_count: int`; `retriever.py` increments a counter.
- **P2-4 — longer IDs.** `_PARENT_ID_LEN` raised 16 → 32; chunker tests updated.

### E-007.1 acceptance criteria
1. Empty `allowed_security_levels` raises `EmptyAuthorizationScopeError` (not silently broad).
2. Empty `groups` cannot match a reserved/crafted security level or payload via the filter.
3. `ParentReader` rejects missing/malformed auth metadata (P1-2 cases).
4. `_rebalance_pair` is present and orphan small parents are rebalanced, not emitted (P1-3).
5. `document_version` is required and part of content-addressed ids (P1-4).
6. E2E uses the real `child_chunk_to_point` mapper end-to-end (P1-5).
7. `_HybridSearchAdapter` is not exported / not importable from `retrieval` (P2-1).
8. Only `ParentAuthorizationError` is swallowed on the parent pass; other errors propagate (P2-2).
9. `RetrievalResult.denied_parent_count` is an int counter (P2-3).
10. `ruff`, `mypy src/agentic_rag_enterprise`, full `pytest` (incl. `tests/baseline/`) all green.

## E-008 Issue Contract (M1 only) — CLOSED at `139df74`
Idempotent ingestion Job + active-version protocol. Full contract (goals, non-goals,
allowed/forbidden paths, §10.10 cross-store rules, crash-point plan) is versioned at
`docs/issue-e008-contract.md` — not duplicated here.

### Allowed paths (M1 only)
- `src/agentic_rag_enterprise/storage/metadata_store.py` — NEW; Metadata DB (control-plane
  source of truth) over `sqlite3`; migration runner.
- `src/agentic_rag_enterprise/storage/parent_store.py` — extend (`deprecate`).
- `src/agentic_rag_enterprise/ingestion/job.py` — NEW; `DocumentManager`/`IngestionJob`
  (parse→chunk→parent store→qdrant→commit→publish) reusing E-007 ported components.
- `migrations/002_add_lifecycle_revision.sql` — NEW; `documents.lifecycle_revision`.
- `config.py` — add `metadata_db_path` (injected).
- `domain/ingestion.py`, `domain/document.py`, `domain/chunk.py` — reuse (no change).
- `security/`, `retrieval/` — reuse only (no behavior change).
- `tests/{unit,integration,security,fixtures}/` — NEW tests; `AGENTS.md`, `docs/issue-e008-contract.md`.

### Forbidden
- No upstream modifications; no target import of upstream paths.
- No second ingestion runtime; extend the E-007 ported chain.
- No Planner / Evidence Store / multi-corpus / reranker (later milestones).
- No logical delete / ACL-tightening (E-010); lifecycle + `lifecycle_revision` mechanism only.

## E-008.1 Issue Contract (M1 only) — CLOSED at this commit
Audit-remediation of E-008 (verdict **Conditional Fail**). Baseline `139df74` + `a3ec258` kept
intact; E-008.1 is a narrow fix commit. Scope is a strict subset of the E-008 allowed paths
(`storage/`, `ingestion/`, `retrieval/`, `tests/...`, `migrations/`, `AGENTS.md`,
`docs/issue-e0081-contract.md`); no upstream modifications, no `config.py`/`domain/` changes.

### P1 fixes (all done)
- **P1-1 — Control-plane active-version gate.** `SecureRetriever` accepts an optional
  `metadata_store`; retrieval drops every hit whose `document_version` is not the Metadata DB's
  current active version for that document (build plan §10.10 #5). E2E + crash-point tests assert a
  deprecated-but-not-deleted version cannot reach the model.
- **P1-2 — Idempotency by `(document, version, content_hash)`, not `job_id`.** Same artifact with a
  different `job_id` returns `ALREADY_INDEXED` (no rework, no active-row flip); same version with
  different content raises `VersionContentConflict` (never overwrites).
- **P1-3 — `base_revision` persisted at acquire; monotonic `MAX(lifecycle_revision)`.**
  `acquire_job` persists the revision captured at acquire; `commit_active_version` CASes against it
  and increments from `MAX(lifecycle_revision)` over ALL versions, so a newer committed version
  wins and old jobs fail closed with `ActiveVersionConflict`.
- **P1-4 — `verify` step before commit.** `run()` adds a `verify` step that confirms expected
  Parent/Qdrant Point IDs exist, identity is consistent, counts match, and the new version is still
  uncommitted (or is our own already-active version on a resumed run).
- **P1-5 — Unified, idempotent compensation.** Pre-commit failure deletes THIS version's data-plane
  artifacts + control-plane chunk records, clears step markers (so a failed job fully re-runs), and
  marks the processing row failed; post-commit publish failure leaves the control-plane active
  version and resumes to publish (never rolls back the active version).
- **P1-6 — `job_id` is an immutable binding.** `validate_job_identity` rejects reuse of a `job_id`
  for a different `(tenant, corpus, document, version)` with `JobIdentityConflict` before any row
  is mutated.
- **P1-7 — Publish deprecates ONLY the version actually replaced.** `previous_active_version` is
  captured at acquire (stable across resume) and persisted; `publish` promotes this version's
  points/parents to `active` and deprecates only that one prior version (never scans all
  non-active rows, never disturbs a concurrent still-processing version).

### Migration
- `migrations/003_e0081_job_metadata.sql` — NEW; `ingestion_jobs.base_revision`,
  `previous_active_version`, `manifest`. `apply_migrations` embeds the marker in the DDL script so
  the schema change and its record commit together (no duplicate-column on next boot).

### Acceptance tests
- `tests/unit/test_metadata_store.py` — monotonic revision, job identity, manifest persisted.
- `tests/unit/test_ingestion_job.py` — content idempotency, base_revision CAS, verify, compensation.
- `tests/integration/test_e008_ingestion_e2e.py` — active-version gate; no stale version leaks.
- `tests/integration/test_e008_crash_points.py` — older job loses race, job identity immutable.
- `tests/baseline/` MUST remain green.
- `ruff`, `mypy src/agentic_rag_enterprise`, full `pytest` all green.

## E-008.2 Issue Contract (M1 only) — CLOSED at this commit
Audit-remediation of E-008.1 (verdict **Conditional Fail**). Baseline `34bfbc4` kept
intact; E-008.2 is a narrow fix commit that closes the real-crash and concurrency
windows the E-008.1 audit left open. Scope is a strict subset of the E-008.1 allowed
paths (`storage/`, `ingestion/`, `retrieval/`, `tests/...`, `migrations/`, `AGENTS.md`,
`docs/issue-e0082-contract.md`); no upstream modifications, no `config.py`/`domain/`
changes.

### P1 fixes (all done)
- **P1-1 — Resume after commit-crash (no silent ALREADY_INDEXED).** `run()` only
  short-circuits to `ALREADY_INDEXED` when the build's lease owner is `SUCCEEDED`;
  an in-flight/crashed build (job still `RUNNING` between `commit_active_version` and the
  `commit` step marker) now RESUMES and finishes publish/finalize. `commit_active_version`
  is idempotent for an already-active version, so a re-run commit with a now-stale
  `base_revision` succeeds instead of raising `ActiveVersionConflict`.
- **P1-2 — `previous_active_version` preserved on post-commit failure.** The
  `set_job_previous_version(job_id, None)` branch in the compensation path is removed;
  recovery's `publish` still needs it to deprecate the replaced version's data plane.
  Crash-point test asserts the old Qdrant points / parents are cleaned on resume.
- **P1-3 — Atomic build lease.** `migrations/004_e0082_build_lease.sql` adds a
  `document_builds` lease (PK `tenant,corpus,document,version`, `owner_job_id`).
  `acquire_job` claims the lease in one `BEGIN IMMEDIATE` transaction; a concurrent
  in-flight build for the same artifact raises `BuildConflict` (no shared-data-plane race
  where compensation deletes the winning build). A terminal owner's build is taken over.
- **P1-4 — `job_id` immutable binding, no TOCTOU.** The identity check is folded into the
  atomic `acquire_job` (single transaction) alongside the lease claim; `validate_job_identity`
  remains a fast pre-check. Two threads opening separate connections converge on one lease
  owner via `BEGIN IMMEDIATE` serialization.
- **P1-5 — `verify` checks true data-plane identity.** `_step_verify` now reads each Parent
  Store entry and each Qdrant point `with_payload=True`, comparing
  tenant/corpus/document/version/parent/chunk identity (not just column presence).
- **P1-6 — Migration atomicity.** `apply_migrations` applies each migration's DDL + the
  `schema_migrations` marker inside one explicit `BEGIN IMMEDIATE … COMMIT` (with `ROLLBACK`
  on error) instead of autocommit `executescript`, so a crash cannot leave a column added
  but unrecorded.
- **P1-7 — Active-version gate is mandatory.** `SecureRetriever.metadata_store` is now a
  required argument (no `None` fail-open bypass). The E-007 PEP/PDP and end-to-end tests
  inject a MetadataStore seeded with the active version.

### P2 fixes (all done)
- **P2-1 — `ALREADY_INDEXED` no longer marks a nonexistent `job_id`.** `run()` only calls
  `mark_job_terminal` when a job row already exists.
- **P2-2 — `publish` scopes parents to THIS build.** `_step_publish` iterates
  `self._parents_list` (the chunker output for this version) instead of scanning the whole
  Parent Store by `document_version`, so a concurrent job's parents are never disturbed.

### Migration
- `migrations/004_e0082_build_lease.sql` — NEW; `document_builds` lease table.

### Acceptance tests
- `tests/unit/test_metadata_store.py` — migration atomicity (fault injection), build lease
  serialization (real dual-thread) + takeover after failure, monotonic revision.
- `tests/unit/test_ingestion_job.py` — ALREADY_INDEXED resumes after commit-crash; verify
  rejects parent identity mismatch; content idempotency; compensation.
- `tests/integration/test_e008_crash_points.py` — publish-failure preserves `previous_active_version`
  and cleans the old data plane on resume; older job loses race; job identity immutable.
- `tests/integration/test_e007_end_to_end.py` + `test_qdrant_hybrid_retrieval.py` — updated to
  inject the mandatory `metadata_store` (E-007 PEP/PDP equivalence preserved).
- `tests/baseline/` MUST remain green.
- `ruff`, `mypy src/agentic_rag_enterprise`, full `pytest` (290) all green.

## E-008.3 Issue Contract (M1 only) — CLOSED at this commit
Audit-remediation of E-008.2 (verdict **Conditional Fail**). Baseline `fd53496` is
kept intact; E-008.3 is a narrow fix commit closing the lease-ownership and
verify-precision windows the E-008.2 audit left open. Scope is a strict subset of
the E-008.2 allowed paths (`storage/`, `ingestion/`, `retrieval/`, `tests/...`,
`migrations/`, `AGENTS.md`, `docs/issue-e0083-contract.md`); no upstream
modifications, no `config.py`/`domain/` changes.

### P1 fixes (all done)
- **P1-1 — Claim-before-mutate + `BuildConflict` never compensates.** `acquire_job`
  now atomically claims the lease, upserts the processing document row, inserts
  the job row, and captures `previous_active_version` in ONE `BEGIN IMMEDIATE`
  transaction — the FIRST mutation a job makes, before any Parent/Qdrant/Chunk
  write. `run()` catches `BuildConflict` separately and returns a typed
  `BUILD_CONFLICT` result WITHOUT compensation, so a loser never deletes the
  winner's deterministic-ID data plane. `_compensate()` additionally re-checks
  lease ownership and silently skips if the lease was taken over.
- **P1-2 — Lease fencing + terminal-state synchronization.** `document_builds`
  gains a `lease_generation` fencing token (migration `005_e0083_lease_generation.sql`).
  Each claim/takeover/resume advances it; the job captures its generation at
  acquire and `_assert_owns_build()` rejects a stale taken-over owner with
  `BuildConflict` before any commit/publish/compensate mutation. Same-owner
  resume atomically resets Job + lease to `running`; `mark_job_terminal` updates
  Job AND lease status in one transaction so a failed build is correctly
  diagnosed as terminal (takeable) vs in-flight.
- **P1-3 — Exact Qdrant payload verification.** `_step_verify` now compares each
  Qdrant point EXACTLY against the chunker output for the version: `tenant_id`,
  `corpus_id`, `document_id`, `document_version`, `parent_id`, `chunk_id`,
  `status == "processing"`, `deprecated is False` (not merely non-empty fields).

### P2 fix (test quality, done)
- **P2 — Precise commit-crash hook.** `test_precise_commit_crash_resumes_publish_and_finalize`
  crashes AFTER `commit_active_version` succeeds but BEFORE the outer `commit`
  step marker is written, then reconstructs and asserts publish+finalize recover
  and clean the replaced version's data plane. A new `_commit_performed` flag
  (not just the step marker) gates compensation so a post-commit crash is never
  rolled back.

### Migration
- `migrations/005_e0083_lease_generation.sql` — NEW; `document_builds.lease_generation`.

### Acceptance tests
- `tests/unit/test_ingestion_job.py` — `test_precise_commit_crash_resumes_publish_and_finalize`
  (P2), `test_build_conflict_loser_never_compensates` (P1-1),
  `test_build_lease_fencing_blocks_taken_over_owner` (P1-2),
  `test_verify_rejects_qdrant_payload_mismatch` (P1-3), `test_verify_rejects_parent_identity_mismatch`.
- `tests/unit/test_metadata_store.py` — takeover advances `lease_generation`; monotonic revision.
- `tests/integration/test_e008_crash_points.py` — `test_taken_over_build_cannot_corrupt_active_version`
  (full-pipeline concurrency regression, P1-2).
- `tests/baseline/` MUST remain green.
- `ruff`, `mypy src/agentic_rag_enterprise`, full `pytest` (294) all green.

## E-008.4 Issue Contract (M1 only) — CLOSED at this commit
Audit-remediation of E-008.3 (verdict **Conditional Fail**). Baseline `b7012cb` is
kept intact; E-008.4 is a narrow fix commit closing three state-transition gaps the
E-008.3 audit left open. Scope is a strict subset of the E-008.3 allowed paths
(`storage/`, `ingestion/`, `tests/...`, `migrations/`, `AGENTS.md`,
`docs/issue-e0084-contract.md`); no upstream modifications, no `config.py`/`domain/`
changes.

### P1 fixes (all done)
- **P1-1 — Deprecated-version idempotency.** `run()` now returns `ALREADY_INDEXED`
  for an already-materialized published version whose `content_hash` matches, for
  BOTH `ACTIVE` and `DEPRECATED` (superseded) statuses (build plan §10.4: same
  `document_id`+`version`+content → skip, no duplicate Chunks/vectors). Previously
  only `ACTIVE` short-circuited; a `DEPRECATED` same-content re-delivery took the
  lease, rewrote the data plane, then failed `verify` and compensated — deleting the
  superseded version's data plane. The `job_id` immutable-binding guard still
  applies (a reused `job_id` bound to a different request fails closed). Refactored
  into `_short_circuit_already_indexed()`.
- **P1-2 — `previous_active_version` bound to the build lease.** The field moved
  from `ingestion_jobs` (per-job) to `document_builds` (per-build-identity). It is
  captured at the FIRST lease claim (current active version) and carried forward on
  takeover/resume — never recomputed against the (already-switched) active version
  (migration `006_e0084_lease_previous_version.sql`). A replacement job taking over
  a post-commit-failed build now inherits the true replaced version and `publish`
  deprecates its data plane correctly.
- **P1-3 — Same-`job_id` concurrency serialized (execution-attempt guard).** A
  build identity `(tenant, corpus, document, version)` may have at most ONE live
  `run()` execution attempt per process (`_claim_build_guard`/`_release_build_guard`
  in `job.py`). A second concurrent `run()` for the same build is a duplicate
  delivery, not a recovery: it returns `BUILD_CONFLICT` before touching the lease or
  data plane, so the in-flight execution keeps its fencing authority and the lease
  generation is never advanced for a duplicate. A retry after the prior execution
  has exited (crashed/finished) is a genuine recovery and proceeds. This separates
  job identity (immutable binding) from execution attempt (lease holder). In-process
  only; cross-process liveness needs a lease timeout/heartbeat (out of scope).

### Migration
- `migrations/006_e0084_lease_previous_version.sql` — NEW; `document_builds.previous_active_version`.

### Acceptance tests
- `tests/integration/test_e008_crash_points.py` — `test_deprecated_version_redelivery_is_idempotent`
  (P1-1), `test_takeover_after_publish_failure_keeps_true_previous_version` (P1-2),
  `test_same_job_id_concurrent_delivery_is_serialized` (P1-3).
- `tests/baseline/` MUST remain green.
- `ruff`, `mypy src/agentic_rag_enterprise`, full `pytest` all green.

## Standard Checks
```bash
# Before starting a task
cd $TARGET_REPO
git status --short
git branch --show-current
git rev-parse HEAD

cd $UPSTREAM_REPO
git status --short
git rev-parse HEAD

# After completing a task
cd $TARGET_REPO
git diff --check
git status --short
```
