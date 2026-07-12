# E-010 Issue Contract (M1) — update, logical delete and ACL-tightening path

Completion of the M1 `ingest -> retrieve -> update -> delete` exit gate (build plan
§3530). Builds on E-008 (idempotent Job + active-version switch + Metadata DB
control plane) and E-009 (parent-store secondary authorization). Closes the
three capabilities deferred from E-008 as non-goals:

- **update** — content change → new version via the existing idempotent ingest
  pipeline (active-version switch + deprecate old); no second ingest runtime.
- **logical delete** — flip a document's status to `deleted` across the three
  planes (Qdrant child points, parent store, Metadata DB) **immediately**, so
  retrieval filters it with no dependency on a background purge (§10.6).
- **physical purge** — after logical delete, remove the document's data plane
  (Qdrant points, parent content, chunk records, raw/parsed artifacts, Metadata
  DB row), scoped strictly to that document's version(s).
- **ACL tightening** — ACL change without content change updates payloads only
  (no re-embedding), across Qdrant + parent store + Metadata DB (§10.7).

Baseline `e40a93c` (E-009 CLOSED) is kept intact; E-010 is a narrow, plan-mandated
capability-completion commit. Scope is a strict subset of M1 paths
(`storage/`, `ingestion/`, `domain/ingestion.py` allowed to extend status helpers,
`retrieval/`/`security/` reuse only, `tests/...`, `docs/`, `AGENTS.md`); no upstream
modifications, no `config.py` changes.

## Goal
Provide `DocumentManager` mutate operations (`update`, `delete` [logical], `purge`
[physical], `tighten_acl`) that re-establish every enterprise authorization
boundary, are idempotent/compensatable, and never let an unauthorized or deleted
resource reach the model input.

## depends_on
- **E-008 / E-008.1–E-008.4** — idempotent `IngestionJob` + active-version protocol +
  Metadata DB as control plane; `DocumentManager.ingest` is the reuse target for
  `update`. `commit_active_version` / `publish` / `deprecate` already exist.
- **E-009** — parent-store secondary authorization (§12.5/§12.9). Logical delete must
  flip parent-store entries so `ParentReader` denies them with `DOCUMENT_DELETED`.
- **E-006** — `SecurityContext` + `build_access_filter`/`resource_passes_filter` PDP/PEP.
  Crucially, `build_access_filter` ALREADY enforces `status == "active"` and
  `deprecated == false` (security/filter.py:74-75), so flipping a child point's
  `status` to `"deleted"` makes retrieval drop it immediately — the §10.6
  "检索立即过滤" requirement is met for free.

## Non-goals (M1 only)
- No second retrieval runtime; extend the E-007/E-008 ported chain.
- No embedding/chunking-version upgrade (build plan §10.8) — separate concern.
- No retention-policy engine; purge accepts a `retention=PurgeNow` default that
  removes the full data plane for M1 (the retention hook is a parameter, not a
  policy implementation).
- No cross-corpus or reranker work (later milestones).

## Design decisions (RECOMMENDED defaults — confirm before implementation)
1. **ACL tightening uses payload-only update.** Add `VectorStore.update_payload(
   name, point_ids, payload)` via Qdrant `set_payload` — NO vectors, so ACL change
   never re-embeds (§10.7). The existing `upsert` is NOT used for ACL changes.
2. **Logical delete is the priority, atomic, fail-safe op.** A single
   `DocumentManager.delete(document_id, ctx)` call flips `status="deleted"` on:
   (a) every Qdrant child point of the document's active version (payload
   `status` + `deprecated=True`), (b) every parent-store entry for that document
   version (mark deprecated/non-active), (c) the Metadata DB `SourceDocument`
   (`status=DELETED`, `deleted_at=now`). Retrieval filtering is then automatic via
   `build_access_filter`. If any plane fails, compensate by re-asserting the prior
   state (idempotent re-run is safe).
3. **Physical purge is scoped and idempotent.** `DocumentManager.purge(document_id,
   ctx)` removes ONLY that document's Qdrant points (by `document_id`+`version`
   scroll), parent contents, `delete_chunk_records`, and the Metadata DB document
   row. Never touches other documents. Re-running purge on an already-purged doc
   is a no-op (not an error).
4. **Update reuses ingest.** `DocumentManager.update(document_id, content, ...)`
   calls the existing `ingest` pipeline with new content → content hash differs →
   new version → active switch + deprecate old (E-008 machinery). No new pipeline.
5. **ACL tightening keeps status ACTIVE.** No new `DocumentStatus` is needed; the
   document stays ACTIVE and only ACL fields (security_level/acl_scope/
   allowed_*/denied_*) change in Qdrant payload + parent metadata + Metadata DB.
   Tightening is prioritized over widening (§10.7) because the canonical PDP
   (`resource_passes_filter`) already gives deny-lists precedence; the new ACL is
   the full source of truth and replaces the old one atomically.
6. **Authorization envelope.** All four operations require a `SecurityContext` and
   MUST fail closed if the caller is not authorized for that `(tenant, corpus,
   document)` (reuse `can_discover_corpus` + tenant match + an ownership/ACL
   write-check). Cross-tenant mutate is rejected (mirrors retrieval deny).

## Allowed paths (M1 only)
- `src/agentic_rag_enterprise/storage/vector_store.py` — add `update_payload`,
  `list_point_ids_by_document(name, tenant_id, corpus_id, document_id, version)`
  (scroll, payload-only, returns ids).
- `src/agentic_rag_enterprise/storage/parent_store.py` — add
  `deprecate_document(document_id, version)` / `delete_document(document_id,
  version)` bulk helpers (reuse `deprecate`/`delete`).
- `src/agentic_rag_enterprise/storage/metadata_store.py` — add
  `set_document_status(document_id, status, deleted_at=None)`,
  `delete_document(document_id)`, and a `list_document_versions` helper if needed.
- `src/agentic_rag_enterprise/ingestion/job.py` — add `DocumentManager.update`/
  `delete`/`purge`/`tighten_acl`; a thin `DocumentMutator` may wrap the store calls
  reusing the existing lease/transaction discipline. `IngestionJob` stays ingest-only.
- `src/agentic_rag_enterprise/domain/document.py` — `SourceDocument` already carries
  `status`/`deleted_at` (and validates deleted→deleted_at); reuse as-is. `domain/
  ingestion.py` DocumentStatus enum is sufficient (DELETED present); NO new status.
- `src/agentic_rag_enterprise/retrieval/` — NO behavior change required (filter
  already enforces status=active); ADD a regression test asserting a
  `status="deleted"` point is filtered.
- `src/agentic_rag_enterprise/security/` — reuse `build_access_filter`/
  `resource_passes_filter` / `can_discover_corpus`; add a write-authorization
  helper if none fits.
- `tests/unit/`, `tests/integration/`, `tests/security/` — new E-010 tests.
- `docs/issue-e010-contract.md` (this file), `AGENTS.md`.

## Forbidden
- No upstream modifications; no target import of upstream paths.
- No `config.py` / `Settings` additions (inject as E-007/E-008 did).
- No second ingestion runtime; `IngestionJob` remains ingest-only.
- MUST NOT flip `status` on any document other than the one being mutated.
- MUST NOT make logical delete depend on background purge for retrieval filtering.
- MUST NOT re-embed on ACL tightening (payload-only).
- MUST NOT weaken E-009 fail-closed parent second-auth.

## Acceptance tests
- `tests/unit/test_metadata_store.py` — `set_document_status`/`delete_document`
  round-trip; `delete_document` is idempotent.
- `tests/unit/test_vector_store.py` — `update_payload` changes ACL payload WITHOUT
  calling the dense/sparse encoder (fake encoder call count unchanged);
  `list_point_ids_by_document` returns only the target document's points.
- `tests/unit/test_document_manager.py` — `update` yields a new active version and
  deprecates the old; `delete` flips all three planes to deleted and is idempotent;
  `purge` removes only the target document's data plane and is idempotent;
  `tighten_acl` updates payloads without re-embedding.
- `tests/integration/test_e010_lifecycle_e2e.py` — same fixture drives
  `ingest -> retrieve -> update -> retrieve(new version) -> delete -> retrieve
  (empty, immediate) -> purge -> re-ingest fresh`. Confirms exit gate (§3530).
- `tests/security/test_e010_authorization.py` — cross-tenant `delete`/`tighten_acl`
  rejected; deny-list tightening removes a previously-authorized user immediately;
  a deleted document's parent is denied with `DOCUMENT_DELETED` via `ParentReader`.
- `tests/retrieval` regression — a `status="deleted"` child point is excluded by
  `build_access_filter` (no code change, just proof).
- `tests/baseline/` MUST remain green (M0/M1 regression).
- `ruff`, `mypy src/agentic_rag_enterprise`, full `pytest` (incl. `tests/baseline/`) all green.

## Acceptance commands
```bash
python -m pytest tests/unit/test_metadata_store.py tests/unit/test_vector_store.py tests/unit/test_document_manager.py tests/integration/test_e010_lifecycle_e2e.py tests/security/test_e010_authorization.py tests/baseline/ -q
ruff check src/agentic_rag_enterprise tests
mypy src/agentic_rag_enterprise
python -m pytest -q
```

## Closure patch (`5828ca1`)

Audit verdict on baseline `79a2cb7`: **CONDITIONAL FAIL** — functional happy path
passes, but three P1 blockers plus one concurrency gap block formal closure. The
closure patch is a narrow, plan-mandated fix (baseline kept intact; no upstream
modifications). Findings and resolutions:

- **P1-1 — Delete/update ordering.** `delete()` only flipped `status`; it did not
  advance `lifecycle_revision`. An in-flight `update` job holding a stale
  `base_revision` could win its `commit_active_version` CAS after the delete and
  resurrect a deleted document. Fix: new `MetadataStore.logical_delete` flips
  `status=deleted` AND advances `lifecycle_revision` inside one `BEGIN IMMEDIATE`
  transaction; `DocumentManager.delete` uses it. Re-running on an already-deleted
  row is idempotent (no double bump).
- **P1-2 — Parent Store isolation.** `deprecate_document`/`delete_document`/
  `update_acl_document` matched only `(document_id, version)`, so a shared Parent
  Store could cross-tenant/cross-corpus mutate. Fix: all three now match
  `tenant_id`+`corpus_id`+`document_id`+`document_version`; `DocumentManager` call
  sites pass the tenant/corpus.
- **P1-3 — Write authorization.** `can_manage_document` only re-checked READ
  access, so any reader could mutate. Fix: it now requires READ access AND
  explicit ownership (named in the ACL allow lists) or admin break-glass.
- **ACL-fencing concurrency gap.** `update_document_acl` preserved
  `lifecycle_revision`, so an `update` job acquired before an ACL tighten could
  publish a new version carrying the pre-tighten ACL. Fix: `update_document_acl`
  now advances `lifecycle_revision`, so the stale job fails its commit CAS.

Acceptance (all green): `tests/unit/test_parent_store.py` (cross-tenant/cross-
corpus isolation), `tests/unit/test_document_manager.py`
(`test_same_tenant_readable_but_not_owner_is_refused`,
`test_delete_advances_revision_blocks_stale_in_flight_update`),
`tests/unit/test_metadata_store.py` (`test_logical_delete_advances_revision_*
`, `test_update_document_acl_advances_lifecycle_revision_for_fencing`),
`tests/integration/test_e010_lifecycle_e2e.py`, `tests/security/
test_e010_authorization.py`, full `pytest` (357), `ruff`, `mypy`.
