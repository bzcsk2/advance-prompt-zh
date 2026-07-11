# Issue E-008.3 — Audit-remediation of E-008.2 (lease ownership + verify precision)

**Milestone:** M1 — Secure single-corpus data vertical slice
**Status:** CLOSED at this commit
**Parent issue:** E-008.2 (CLOSED at `fd53496`) — audit verdict **Conditional Fail**
**Baseline preserved:** `fd53496` is kept intact; E-008.3 is a narrow fix commit.

## Scope

Strict subset of the E-008.2 allowed paths:
`src/agentic_rag_enterprise/{storage,ingestion,retrieval}/`,
`tests/{unit,integration,security,fixtures}/`, `migrations/`, `AGENTS.md`,
`docs/issue-e0083-contract.md`.

No upstream modifications. No `config.py` / `domain/` changes. No new behaviors
beyond the three P1 windows + the P2 test-precision note below.

## Audit findings (from the E-008.2 review)

1. **P1-1 — Lease rejector still mutates Document and runs shared-data compensation.**
   `_step_acquire` did `upsert_document(processing)` THEN `acquire_job()`; a
   `BuildConflict` loser already mutated shared Document and then hit the generic
   exception branch → `_compensate()` deleted the owner's deterministic-ID data.
2. **P1-2 — Failed owner retry stays FAILED; lease can be taken over during recovery.**
   `acquire_job` read the owner's original `ingestion_jobs.status` and returned
   without resetting to RUNNING; `mark_job_terminal` updated only `ingestion_jobs`.
   A FAILED owner retrying could be taken over by a concurrent Job.
3. **P1-3 — Qdrant verify does not check `parent_id`/`chunk_id` exact equality.**
   `_step_verify` only asserted `parent_id`/`chunk_id` were non-empty, so a
   tampered payload (e.g. `parent_id="wrong-parent"`) passed verification.
4. **P2 — Real commit-crash test still not precise state simulation.**
   `test_already_indexed_resumes_after_commit_crash` completed a full ingest first
   and then手工 set the job RUNNING, instead of crashing precisely between
   `commit_active_version` success and the `commit` step marker.

## Remediation

### P1-1 — Claim-before-mutate + `BuildConflict` never compensates
- `MetadataStore.acquire_job` now performs, in ONE `BEGIN IMMEDIATE` transaction:
  lease claim, processing document-row upsert (only if the row is absent or
  uncommitted; an already-active/deprecated row is preserved), job-row insert,
  immutable-identity check, and `previous_active_version` capture. This is the
  first mutation a job makes, before any Parent/Qdrant/Chunk write.
- `IngestionJob.run` catches `BuildConflict` in a separate `except` branch and
  returns a typed `IngestionStatus.BUILD_CONFLICT` result with
  `error_code="build_conflict"` — it NEVER calls `_compensate`.
- `IngestionJob._compensate` re-checks lease ownership (`_assert_owns_build`) and
  silently returns if the lease was taken over, as a second safety net.

### P1-2 — Lease fencing + terminal-state synchronization
- New column `document_builds.lease_generation` (migration `005_e0083_lease_generation.sql`).
- Each claim / takeover / resume advances `lease_generation`. The job captures its
  generation at acquire (`self._lease_generation`).
- `IngestionJob._assert_owns_build()` compares the live lease owner **and**
  `lease_generation` against the captured values; a mismatch raises `BuildConflict`
  before any commit / publish / compensate mutation.
- Same-owner resume atomically resets Job + lease to `running` and clears
  `finished_at` / `error_*`. `mark_job_terminal` updates Job AND lease status in
  one transaction (`done` for succeeded, `failed` for failed), so a failed build
  is correctly diagnosed as terminal (takeable) vs in-flight.

### P1-3 — Exact Qdrant payload verification
- `_step_verify` builds `expected_by_point_id = {child_point_id(child.child_id): child
  for child in self._children_list}` and compares each retrieved point EXACTLY:
  `tenant_id`, `corpus_id`, `document_id`, `document_version`, `parent_id`,
  `chunk_id`, `status == "processing"`, `deprecated is False`. A tampered payload
  is rejected with `RuntimeError`.

### P2 — Precise commit-crash hook
- New flag `IngestionJob._commit_performed`, set only after
  `commit_active_version` returns. `run`'s `except` compensates only when
  `not self._commit_performed` (not merely when the `commit` step marker is
  absent), so a crash after the active-version switch is never rolled back.
- `test_precise_commit_crash_resumes_publish_and_finalize` overrides `_step_commit`
  to run the real commit then raise; it then reconstructs the job and asserts
  publish + finalize recover and clean the replaced version's data plane.

## Acceptance tests
- `tests/unit/test_ingestion_job.py`:
  - `test_precise_commit_crash_resumes_publish_and_finalize` (P2)
  - `test_build_conflict_loser_never_compensates` (P1-1)
  - `test_build_lease_fencing_blocks_taken_over_owner` (P1-2)
  - `test_verify_rejects_qdrant_payload_mismatch` (P1-3)
  - `test_verify_rejects_parent_identity_mismatch` (existing, retained)
- `tests/unit/test_metadata_store.py`: `test_build_lease_takeover_after_failed_owner`
  asserts `lease_generation` advanced; monotonic revision retained.
- `tests/integration/test_e008_crash_points.py`:
  `test_taken_over_build_cannot_corrupt_active_version` (full-pipeline regression, P1-2).
- `tests/baseline/` MUST remain green.
- `ruff`, `mypy src/agentic_rag_enterprise`, full `pytest` (294) all green.

## Non-regression notes
- E-008.2 fixes retained and valid: migration atomicity (P1-6), mandatory
  `metadata_store` active-version gate (P1-7), commit idempotent resume (P1-1),
  `previous_active_version` preserved on post-commit failure (P1-2), exact parent
  verify identity (P1-5 partial), `publish` scopes parents to `self._parents_list`
  (P2-2), `ALREADY_INDEXED` job-row guard (P2-1), atomic lease + `BuildConflict`
  (P1-3/P1-4).
