# E-008.2 Issue Contract — E-008.1 Consistency Remediation (narrow)

- **Parent issue:** E-008.1 (`34bfbc4`) — CONDITIONAL FAIL on code-level audit.
- **Milestone:** M1 — Secure single-corpus data vertical slice.
- **Verdict basis:** `agentic-rag-enterprise-build-plan.md` §10.4, §10.5, §10.9, §10.10.
- **Scope:** narrow remediation of E-008.1 only. **No rollback of `34bfbc4`.** No new
  runtime, no Planner/Evidence/multi-corpus. Reuses MetadataStore, IngestionJob,
  VectorStore, ParentStore, chunker, retriever.

```yaml
id: E-008.2
milestone: M1
depends_on: [E-008.1]
allowed_paths:
  - migrations/004_e0082_build_lease.sql                # NEW
  - src/agentic_rag_enterprise/storage/metadata_store.py  # extend (atomic lease, atomic migration, BuildConflict)
  - src/agentic_rag_enterprise/ingestion/job.py           # extend (resume logic, verify identity, publish scope, prev-version)
  - src/agentic_rag_enterprise/retrieval/retriever.py     # metadata_store now required
  - tests/unit/test_ingestion_job.py                       # extend
  - tests/integration/test_e008_ingestion_e2e.py          # extend
  - tests/integration/test_e008_crash_points.py           # extend
  - tests/unit/test_metadata_store.py                     # extend
  - tests/integration/test_e007_end_to_end.py             # extend (inject metadata_store)
  - tests/integration/test_qdrant_hybrid_retrieval.py     # extend (inject metadata_store)
  - tests/fixtures/__init__.py                            # extend (active_metadata_store helper)
  - docs/issue-e0082-contract.md
  - AGENTS.md
forbidden_paths:
  - /vol4/Agent/agentic-rag-for-dummies                   # upstream read-only
  - agents/ graph/ api/ observability/ evals/              # not in scope
  - config.py / domain/                                    # no behavioral change
rollback: "revert commit; drop migration 004 (document_builds CREATE IF NOT EXISTS is idempotent)"
```

## Audit findings remediated (from E-008.1 CONDITIONAL FAIL)

| ID  | Finding | Fix |
|-----|---------|-----|
| P1-1 | Real crash between `commit_active_version` success and the `commit` step marker → version ACTIVE + job RUNNING → next delivery short-circuits to `ALREADY_INDEXED`, leaving publish/finalize never run. | `run()` only `ALREADY_INDEXED` when the build-lease owner is `SUCCEEDED`; otherwise resume. `commit_active_version` idempotent for an already-active version. |
| P1-2 | Post-commit failure path `set_job_previous_version(job_id, None)` dropped the replaced version, so recovery's `publish` could not clean the old data plane. | Removed the `None` reset; `previous_active_version` is preserved across failure so resume deprecates the old data plane. |
| P1-3 | Two concurrent same-`(doc,version,content)` jobs produce identical deterministic IDs; the loser's unified compensation deletes the winner's active data plane. | `document_builds` lease acquired atomically in `acquire_job`; a concurrent in-flight build raises `BuildConflict` instead of racing. |
| P1-4 | `job_id` immutable binding had a TOCTOU: `validate_job_identity` then `acquire_job` were separate. | Identity check folded into the single atomic `acquire_job` transaction (lease claim + job-row insert + identity verify). `validate_job_identity` kept as a fast pre-check. |
| P1-5 | `verify` only checked `parent_id in store` and Qdrant `with_payload=False`; no true identity check. | `verify` reads each Parent Store entry and each Qdrant point `with_payload=True`, comparing tenant/corpus/document/version/parent/chunk identity. |
| P1-6 | `apply_migrations` used `isolation_level=None` + `executescript`, so DDL and the `schema_migrations` marker were not atomic; crash → duplicate-column on reboot. | Each migration now runs DDL + marker inside one explicit `BEGIN IMMEDIATE … COMMIT` (with `ROLLBACK` on error). |
| P1-7 | `SecureRetriever.metadata_store` was optional → a public entry could bypass the active-version gate (fail-open). | `metadata_store` is now a **required** argument. E-007 PEP/PDP and E2E tests inject a MetadataStore seeded with the active version. |
| P2-1 | A `job_id` different from the owning job hitting the `ALREADY_INDEXED` path called `mark_job_terminal(job_id)` for a row that did not exist. | `run()` only marks terminal when a job row already exists. |
| P2-2 | `_step_publish` scanned the Parent Store by `document_version` only, not tenant/corpus/document. | `_step_publish` iterates `self._parents_list` (this build's chunker output). |

## Design invariants

1. **One in-flight build per `(tenant, corpus, document, version).** The `document_builds`
   lease is the authoritative ownership record. `acquire_job` claims it inside
   `BEGIN IMMEDIATE`; concurrent in-flight owners raise `BuildConflict`. A terminal owner's
   build is taken over by a re-delivered job.
2. **Resume, never skip.** A committed-but-unpublished version is recovered by resuming the
   pipeline (publish/finalize), not by short-circuiting to `ALREADY_INDEXED`. The build-lease
   owner status (`SUCCEEDED`) is the discriminator, not just the document being active.
3. **Idempotent commit.** `commit_active_version` is a no-op (returns current revision) when
   the requested version is already active, so a resume re-running commit with a stale
   `base_revision` does not fail closed.
4. **Atomic migration.** DDL + marker are one transaction; a crash rolls back both, so the next
   boot re-applies cleanly.
5. **Mandatory control-plane gate.** `SecureRetriever` always applies the active-version gate;
   there is no `None` bypass.
6. **Preserved `previous_active_version`.** Recovery's publish uses it to deprecate the replaced
   version's data plane (Qdrant points + parents).

## Acceptance tests (all green)

- `tests/unit/test_metadata_store.py` — migration atomicity (fault injection),
  build-lease dual-thread serialization + takeover, monotonic revision.
- `tests/unit/test_ingestion_job.py` — `ALREADY_INDEXED` resumes after commit-crash;
  `verify` rejects parent identity mismatch; content idempotency; compensation.
- `tests/integration/test_e008_crash_points.py` — publish-failure preserves
  `previous_active_version` and cleans the old data plane on resume; older job loses race;
  job identity immutable.
- `tests/integration/test_e007_end_to_end.py` + `test_qdrant_hybrid_retrieval.py` — updated to
  inject the mandatory `metadata_store` (E-007 PEP/PDP equivalence preserved).
- `tests/baseline/` MUST remain green.
- `ruff`, `mypy src/agentic_rag_enterprise`, full `pytest` (290) all green.
