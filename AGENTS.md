# AGENTS.md — Agentic RAG Enterprise

## Implementation Spec
`docs/agentic-rag-enterprise-build-plan.md`

## Current Milestone & Issue
- Milestone: **M7** — Runtime hardening (`E-022` → `E-024`); M6 / E-021 **CLOSED / ACCEPTED at `c6c3e6b`**; **M7 / E-022** **CLOSED / ACCEPTED at `cd4ddb2`**
- Current Issue: **E-023** — Persistent Checkpoint + re-authorization on resume (build plan Milestone 7)
- Issue: **E-017** — Typed `QueryPlan` / `PlanStep` contract + DAG Validator — **CLOSED /
  ACCEPTED at `398f059`** (acceptance re-audit `33c...` passed: 10-check independent
  re-verification — schema invariants, DAG integrity for required+optional edges, binding
  semantics, permission redaction, registry-as-truth-source, capability/read-only
  boundary, budget math, repair limit, pure control-plane boundary, E-018 readiness). The
  re-audit also hardened `PlanViolation.detail` to `Field(exclude=True, repr=False)` so
  the unauthorized Corpus name cannot leak via `str()`/`repr()` into logs. Full contract
  at `docs/issue-e017-contract.md`.
- Issue: **E-018** — Controlled Executor + dependent multi-hop — **CLOSED / ACCEPTED at
  `4d072bd`** (the E-018 contract at `docs/issue-e018-contract.md` was accepted; the
  executor, `StepResult` / `PlanExecutionResult`, `AtomicToolBudget`, `ToolRegistry`,
  `RetrieverTool` and tests were implemented at `4d072bd` — "E-018: real RetrieverTool
  two-hop + full regression pass" — on top of the amended contract `5d02d99` /
  `2027102`). Consumes an accepted `QueryPlan`: `StepResult` (frozen state machine:
  pending/running/succeeded/failed/timed_out/skipped_dependency/budget_exhausted),
  parallel-ready scheduling in topological layers, required/optional dependency + binding
  semantics, per-step timeout, **atomic** shared Tool-Call budget (`AtomicToolBudget` with a
  single `try_reserve` API, no double-count; `PlanStep.max_tool_calls` is runtime cap;
  multi-corpus step reserves N units per attempt), exactly one retry (initial + 1, retryable types
  only: `RetrievalBackendError`/`ConnectionError`/`TimeoutError` + registered transient
  infra; retry blocked when `max_tool_calls=1`), fail-closed security degradation, and a final
  `PlanExecutionResult` with deterministic `evidence_ids` first-occurrence dedup and **no**
  whole-execution `error_code`/`message` (fail-closed → `PlanExecutionError`). The amendment
  also froze: `facts.<id>.value` == `RequiredFact.description`; a `ToolSpec` (`ToolRegistry.get()`
  returns `(Tool, ToolSpec)`; `ToolSpec.input_model` uses `is_required()` for missingness; `ToolSpec.output_models`
  maps all four `OutputSchemaId` values to their output model) that decides optional-binding
  missingness; the distinction
  between local data `binding_error` (step-only failure) and security binding failure
  (whole-execution fail-closed); the full `PlanExecutionResult` schema + "usable result"
  (Evidence-based) definition; and the deterministic `RetrieverTool` Evidence→`entity`/`spec`
  projection (no LLM; `retrieval_score=None`→`0.0`, sort: score desc, authority desc, evidence_id asc;
  empty list returns empty outputs). Full contract at `docs/issue-e018-contract.md`.
- Prior milestone **M4 / E-015 -> E-016** — Multi-Corpus retrieval — **CLOSED / ACCEPTED**
  at `033c8e2` (E-016 second re-audit passed). E-015 (Corpus/Capability Registry
  + three Corpus fixtures + permission-safe discoverability) CLOSED; E-016
  (permission-aware soft router + cross-Corpus retrieval merge + dedup) CLOSED. Detailed
  E-016 closure notes retained in the issue history below.
- Issue: **E-011** — Evidence snapshot store and required deduplication — merged at
  `4b32b34`; acceptance remediation completed in the current change set (current-policy
  reauthorization after ACL tightening/delete, per-owner ACL persistence, fuzzy-dedup
  replacement, and executable acceptance commands). Full contract at
  `docs/issue-e011-contract.md`.
- Issue: **E-012** — Single-corpus Fast Path and one-pass sufficiency decision — CLOSED
  (local commit `fbb24f8`; acceptance verdict **PASS**). Adds `retrieval/fast_path.py`:
  `run_fast_path` calls `SecureRetriever.retrieve_evidence` exactly once and applies the
  deterministic baseline sufficiency rule (≥1 Evidence → `sufficient`; 0 → `insufficient`,
  downstream must abstain). Frozen, validated `FastPathResult` (`sufficiency` + `stop_reason`
  + derived `is_sufficient`/`should_abstain`) and a typed `FastPathBackendError` so a
  retrieval fault is never relabelled as "no answer". Full contract at
  `docs/issue-e012-contract.md`.
- Issue: **E-013** — AnswerEnvelope, citation rendering, single key-claim support
  verification, and conservative refusal — implemented and in acceptance remediation
  (4 P1 fixes applied: fail-closed tenant/corpus binding via `TenantBindingError`;
  unsupported / evidence-less / unresolved claims removed and their facts excluded from
  `answer_markdown`, with missing/empty Claim maps failing closed to a safe partial response;
  `Claim`/`Citation` frozen + validator checks `Citation.evidence_id`;
  `conservative_refusal` rejects a `sufficient` result and the envelope locks
  `abstained` ⇒ `stop_reason == no_evidence`). Adds `answer/`: `AnswerEnvelope` (deeply
  frozen, validated), `Claim`/`Citation` (frozen), `render_citations`/`format_citation_panel`
  (immutable snapshot refs), `verify_claims`, `build_answer_envelope`/`conservative_refusal`
  driven by the E-012 `FastPathResult`. Whole-tree `ruff format --check .` gate now
  CLEAN (the 2 last drifting pre-existing test files reformatted; no behavior change).
  Local commits `4cb0fb8` (4 P1 fixes) + `2770971` (fail-closed: answer derived only from
  verified claims, caller prose never returned) + `b7ed855` (whole-tree format gate clean).
  Full contract at `docs/issue-e013-contract.md`.
- Issue: **E-014** — shared chat application service, synchronous `/v1/chat` contract, and a
  minimal Gradio adapter — **CLOSED** (acceptance remediation committed at `5084b2f`; run-chain
  verified end-to-end against the real default app). The original
  implementation passed unit tests but failed acceptance on 4 P1 + 1 P2 findings. Fixed:
  (P1-1) the default `/v1/chat` is now runnable — `get_chat_service` returns a shared, in-process
  `DefaultServiceContainer` (in-memory Qdrant, deterministic encoders, a hermetic synthesis model
  that registers `ClaimExtraction`, and a storage stack **shared with the ingestion pipeline**), so
  ingest → chat works end-to-end with no external dependency; (P1-2) `ChatRequest` carries only
  `query` + `corpus_id` — the `SecurityContext` is built from **trusted request headers**
  (gateway-injected), never the client body, so a client cannot assert `tenant_id` / `is_admin`;
  (P1-3) error handlers return only **fixed generic** messages and log the real exception
  internally, so no evidence/tenant id leaks; (P1-4) Gradio now renders citations / Evidence
  snippets / a single-corpus entry (not just `answer_markdown`) and ships a real smoke test
  (gradio installed as an optional extra); (P2) milestone order corrected below.
  `services/chat_service.py` `ChatService` wires E-012 `run_fast_path` → E-013
  `build_answer_envelope` / `conservative_refusal`, calls the LLM only for claim extraction
  (structured `ClaimExtraction`), and never returns the raw LLM draft (E-013 fail-closed).
  `api/routes/chat.py` `POST /v1/chat` is an adapter that injects the runtime `SecurityContext`
  and returns the `AnswerEnvelope` (no SSE, no `denied_reasons` leak); `ui/gradio_app.py` is
  import-safe (lazy gradio). Backend / model faults propagate as typed errors, never as a refusal.
  **Internal MVP (E-011 → E-014) run-chain complete (verified end-to-end).** Full contract at
  `docs/issue-e014-contract.md`.
- Issue: **E-019** — Required-Fact Coverage judge + explicit state transitions — **implemented**
  (current change set). Adds the Stage A `DeterministicCoverageJudge` (lexical overlap + negation
  heuristic) and Stage B `DeterministicClaimEvidenceVerifier` behind a pluggable `Judge` protocol,
  exposing per-fact `FactStatus` and a `SufficiencyResult` on `AnswerEnvelope`; maps coverage →
  explicit state transitions (`sufficient`→complete, `partially_sufficient`→partial + missing list,
  `contradicted`→conflicted, `insufficient`/`policy_blocked`→abstain). `answer()` stays single-pass
  (delegates to `answer_with_iteration(max_rounds=1, judge=None)`) so E-014 behaviour is unchanged.
  Full contract at `docs/issue-e019-contract.md`.
- Issue: **E-020** — Bounded gap retrieval + no-new-evidence stop policy — **implemented**
  (current change set; acceptance-remediation applied). Reuses the E-019 `Judge`; adds `GapPlanner`
  (queries ONLY for `missing`/`partially_supported` — `not_retrievable` is explicitly EXCLUDED, as is
  `contradicted`/`ambiguous`/`policy_blocked`/`supported`; repeats of already-executed queries are
  dropped) and `StopPolicy` (`sufficient` / `no_new_evidence` / `all_sources_exhausted` / `max_rounds`
  / `budget_exhausted` / `tool_unavailable`) driving a bounded 1–3 round loop in
  `ChatService.answer_with_iteration` (accumulates Evidence, re-judges, synthesizes only after the
  loop). When `GapPlanner` has no remaining candidate query the loop stops immediately with
  `all_sources_exhausted` (it does NOT spin an empty re-judge round). A fresh Evidence id that carries
  already-seen text/version is NOT counted as a gain (P2-2). A judge fault degrades conservatively to an
  abstain (never a fabricated complete answer). Adds a deterministic eval harness
  (`evals/dataset.py`, `evals/runner.py`, `false_sufficient`, `judge_timeout_degradation`,
  `evals/data/m3_v1.json`); the report exercises `judge_timeout_degradation` via a real injected
  `JudgeTimeoutError` (not on healthy envelopes). Full contract at `docs/issue-e020-contract.md`.
- Next milestones (after the Internal MVP):
  - **M3 / E-019 → E-020** — Evaluation & grounding-judge MVP — **implemented**; committed and pushed
    (latest acceptance remediation `325dad0` → follow-up remediation). `no_new_evidence` is reachable
    end-to-end via one-new-gap-query-per-round; `gap_rounds`/`iterations` equal executed rounds.
  - **M4 / E-015 → E-016** — Multi-Corpus retrieval (build plan §9 / Milestone 4). **E-015
    (Corpus/Capability Registry + three Corpus fixtures + permission-safe discoverability) CLOSED.**
    **E-016** (permission-aware soft router + cross-Corpus retrieval merge + dedup) —
    original implementation CLOSED at `67280a1`; a code-level audit returned **FAIL**
    (4 P1 + 3 P2), remediated and a **second** re-audit also returned **FAIL** (4 P1
    residual, focus on fallback + fault classification), now CLOSED at the current
    commit. Adds     `corpus/router.py` (`CorpusRouter` — deterministic, model-free,
    **query-sensitive** ranking per build plan §9.3: `score = 0.7*relevance +
    0.3*(authority/100)` clamped `[0,1]`, `relevance` = normalized stopword-filtered
    token overlap of the query vs the corpus name/description/domain/id/capabilities;
    emits `route_confidence` (high/medium/low) + `fallback_search`; `fallback_candidates`
    is exposed ONLY for a high-confidence Top-1 route with no explicit `limit`
    (medium/low/truncated routes stay empty, so a hard `router_limit` cap cannot be
    bypassed); applies high→Top-1 / medium→Top-2 / low→Top-3+fallback; input is ONLY
    `registry.resolve_candidates`, never the full corpus map) and
    `retrieval/multi_corpus.py` (`MultiCorpusRetrieval` runs the existing
    `SecureRetriever.retrieve_evidence` once per selected corpus with the shared
    `SecurityContext`; `merge_evidence` now returns a `MergeResult` with two layers —
    stable `evidence_id` first-occurrence dedup THEN cross-id same-`(text_hash,
    document_id, document_version)` fold to higher authority; `contributing_corpora`
    credits only corpora that emitted a surviving primary/folded Evidence, so a corpus
    whose raw snapshots were all stable-id duplicates is not over-counted (P2-1);
    different version never folded, fully deterministic). **Explicit fault boundary**:
    `retrieval/backend_fault.py` classifies a per-corpus failure; only
    `RetrievalBackendError` (or a curated infra set: `ConnectionError`/`TimeoutError`/
    qdrant `UnexpectedResponse`/`ResponseHandlingException`) becomes a
    `CorpusRetrievalFault` — **never** `ValueError`/`TypeError`/programming bugs;
    security/authorization/binding errors (`CorpusNotDiscoverableError`,
    `ParentAuthorizationError`, `EmptyAuthorizationScopeError`, `TenantBindingError`)
    propagate fail-closed even when a sibling succeeds; a total fault
    (`len(faults)==len(corpora)`) raises while a single-fault-plus-empty-sibling does
    not; every returned snapshot is re-bound to the requested tenant/corpus
    (`TenantBindingError` on mismatch); `retrieval_calls` tracks the true call count.
    `services/chat_service.py` `answer_multi_corpus(query, ctx, *, corpus_ids=None,
    router_limit=None)` sources the retrieval `CorpusConfig` ONLY from `registry.get(...)`
    (routed + explicit + fallback; legacy `_resolve_corpus` never on this path); fallback
    expansion is strictly scoped to **`corpus_ids is None` AND `router_limit is None` AND
    `route_confidence == high` AND primary empty** — on which it queries ONLY the new
    Top-2 fallback corpus (never re-queries the primary) and merges results so
    `tool_calls` equals the true call count (P1-1/P1-2); security/binding errors are
    re-raised in their original type before any wrapping (never relabelled as
    `FastPathBackendError`); a partial fault *with no surviving evidence* raises rather
    than emitting a plain `no_evidence` abstain (P1-4); otherwise it routes → retrieves →
    single-pass synthesizes via `answer/builder.py:build_multi_corpus_envelope` and on
    partial fault degrades the envelope from complete/high to partial/medium with an
    explicit partial-retrieval `limitations` entry (never unconditionally complete);
    `tool_calls` = `retrieval_calls`. The single-corpus `answer()` / `answer_with_iteration`,
    the E-012 Fast Path and the E-013/E-019/E-020 abstain lock are unchanged.
    `AnswerEnvelope.corpora_used` reflects only corpora that actually contributed Evidence.
    Full contract at `docs/issue-e016-contract.md`. Explicitly **excludes** Planner DAG,
    Required-Fact Judge, iteration, authority/freshness conflict arbitration, and
    SQL/API/graph capability (    those
    are M5 / E-017 → E-018).
- **M5 / E-017 → E-018** — Controlled Planner and dependent multi-hop — **CLOSED**.
  E-017 (Typed `QueryPlan` / `PlanStep` + DAG Validator) CLOSED / ACCEPTED at `398f059`.
  E-018 (controlled Executor + `RetrieverTool` two-hop) implemented and **CLOSED / ACCEPTED
  at `4d072bd`** ("E-018: real RetrieverTool two-hop + full regression pass") on top of the
  amended `docs/issue-e018-contract.md` (`5d02d99` / `2027102`). Full Planner/DAG contract
  at `docs/issue-e017-contract.md`; Executor contract at `docs/issue-e018-contract.md`.
- **M6 / E-021** — Temporal scope, source authority & conflict — **CLOSED / ACCEPTED at
  `c6c3e6b`** (final acceptance verdict **PASS**; the three P1 defects from the `d37592e`
  review are closed). Implementation at `c6c3e6b` adds `domain/temporal.py`
  (`TemporalScope`, deterministic temporal parser + `filter_by_temporal_scope`),
  `evidence/conflict_resolver.py` (`ConflictResolver` — four auto-resolution rules; unresolvable →
  `CONTRADICTED`, emits `ConflictReport.conflict_status`), the `AnswerEnvelope.conflict_report`
  extension, a deterministic `render_conflict_answer` (claims cleared, both sides' sources +
  effective times listed, no single conclusion), and `ChatService` wiring on all three paths:
  temporal filter + conflict stage run BEFORE the Coverage Judge each round, `TemporalScope`
  parsed once at request entry, zero-evidence after filtering short-circuits to conservative
  refusal (`insufficient`/`abstained`/`no_evidence`, NO model call), and `CONTRADICTED` stops
  iteration immediately (no further gap retrieval). `tests/integration/test_e021_evidence_pipeline.py`
  extends with 5 P1 tests. Planner core unchanged; M7 not entered in the acceptance commit. Full
  contract at `docs/issue-e021-contract.md`.
- Issue: **E-022** — Reconciler + purge + index migration + rollback (build plan Milestone 7) —
  **CLOSED / ACCEPTED at `cd4ddb2`**. All 4 P1 defects closed: **P1-1** v2 index builds only the
  active version (`iter_active_child_chunks` JOINs active, non-deprecated documents; deprecated and
  logically-deleted content excluded); **P1-2** `DocumentManager.rollback_active_version` re-syncs
  the real data plane (Qdrant payload + Parent Store status/`deprecated` flip via `_sync_version_status`,
  with a `rollback_data_plane_sync_failed` control-plane finding + re-raise on failure); **P1-3**
  index switch gains a per-corpus lease (`index_switch_leases`) + `BEGIN IMMEDIATE` atomic pointer flip
  (`set_active_collection_atomic`) with registry-update compensation (`_switch_pointer` reverts the
  persisted pointer on live-registry failure, `IndexSwitchConflict` on contention); **P1-4** the
  reconciler rebuilds missing Parent Store entries from child chunk rows (parents are never persisted,
  so text is reconstructed by `parent_id` grouping) and realigns a drifted live-registry pointer to
  the persisted truth (`registry_mismatch`). Gates at acceptance: **766 passed / 1 skipped**, ruff
  check + `ruff format --check` clean, mypy clean. Full contract at `docs/issue-e022-contract.md`.
- Issue: **E-023** — Persistent checkpoint + re-authorization on resume (build plan
  Milestone 7) — **CONTRACT OPEN** (implementation pending). Checkpoints the
  `answer_with_iteration` loop to a `run_checkpoints` table on the Metadata DB and
  re-authorizes on resume against current ACLs/active version/corpus discoverability
  (fail closed: drops any evidence the principal can no longer read; refuses
  cross-principal / stale-`policy_version` / undiscoverable-corpus resume). Full
  contract at `docs/issue-e023-contract.md`. E-024 remains OPEN within M7.
- Issue: **E-007** — Port parent-child chunking + hybrid retrieval from upstream (algorithm only, enterprise security envelope) — CLOSED at `ccb52dc`.
- Issue: **E-007.1** — Audit-remediation of E-007 (5 P1 + 4 P2 findings) — CLOSED at `b0dbf6f`.
- Issue: **E-008** — Implement idempotent ingestion job and active-version protocol (M1) — CLOSED at `139df74`.
- Issue: **E-008.1** — Audit-remediation of E-008 (7 MUST violations) — CLOSED at this commit; full contract at `docs/issue-e0081-contract.md`.
- Issue: **E-008.2** — Audit-remediation of E-008.1 (real-crash + concurrency windows) — CLOSED at this commit; full contract at `docs/issue-e0082-contract.md`.
- Issue: **E-008.3** — Audit-remediation of E-008.2 (lease-ownership + verify precision) — CLOSED at `b7012cb`; full contract at `docs/issue-e0083-contract.md`.
- Issue: **E-008.4** — Audit-remediation of E-008.3 (state-transition gaps: deprecated-version idempotency, `previous_active_version` transfer across takeover, same-`job_id` concurrency) — CLOSED at this commit; full contract at `docs/issue-e0084-contract.md`.
- Issue: **E-009** — Add parent-store secondary authorization (M1): complete §12.5 Parent 二次权限校验 + §12.9 distinct failure semantics (`PARENT_NOT_FOUND`/`PARENT_NOT_AUTHORIZED`/`DOCUMENT_DELETED`/`VERSION_MISMATCH`), no un-authorized direct read. CLOSED at `74c298d`; closure patch CLOSED at this commit — `RetrievalResult.denied_reasons` marked `Field(exclude=True)` so §12.9 telemetry is never serialized to the user (build plan §12.9 "avoid leaking existence"). Full contract at `docs/issue-e009-contract.md`.
- Issue: **E-010** — Add update, logical delete and ACL-tightening path (M1): complete the `ingest -> retrieve -> update -> delete` exit gate (§3530). CLOSED at `79a2cb7`; first closure patch CLOSED at `5828ca1` (audit verdict CONDITIONAL FAIL → three P1 blockers + one ACL-fencing gap fixed; baselines `79a2cb7` kept intact); second closure patch CLOSED at `5016bfa` (second review CONDITIONAL FAIL on a residual delete/tighten race window → control-plane revision bump now FIRST and atomic for both `delete` and `tighten_acl`, plus `is_acl_tightening` widening guard; baselines `79a2cb7`/`5828ca1` kept intact). E-010 now fully CLOSED.
  - **P1-1 (delete/update ordering)** — new `MetadataStore.logical_delete` flips `status=deleted` AND advances `lifecycle_revision` atomically (`BEGIN IMMEDIATE`), so an in-flight `update` job captured `base_revision` before the delete fails its `commit_active_version` CAS (`ActiveVersionConflict`) and cannot resurrect a deleted document (build plan §10.10 #8). `DocumentManager.delete` uses it (no longer `set_document_status`).
  - **P1-2 (Parent Store isolation)** — `deprecate_document`/`delete_document`/`update_acl_document` now match `tenant_id`+`corpus_id`+`document_id`+`document_version`, never just `(document_id, version)`, so a shared Parent Store cannot cross-tenant/cross-corpus mutate.
  - **P1-3 (write authorization)** — `can_manage_document` now requires READ access AND explicit ownership (named in the ACL allow lists) or admin break-glass; read access alone no longer confers write (build plan §10.6/§10.7).
  - **ACL-fencing gap** — `update_document_acl` now advances `lifecycle_revision` (no longer preserves it) so an `update` job acquired before an ACL tighten fails its commit CAS and cannot publish a pre-tighten ACL.
  - Unified E-010 implementation with immediate full purge (no soft-delete tombstone / no retention window): `DocumentManager.update` (reuses `ingest`), `delete` (3-plane logical flip, immediate so retrieval filters with no background dependency), `purge` (scoped physical removal across Qdrant + Parent Store + Metadata child rows, refuses non-deleted, idempotent), `tighten_acl` (payload-only via `update_payload`/`update_acl_document`/`update_document_acl`, NO re-embedding). `get_document_latest` tie-break is deterministic (`lifecycle_revision DESC, rowid DESC`). Full contract at `docs/issue-e010-contract.md`.
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

## Fixed Commits
- M2 / E-011 merge baseline: `79b087f` (main)
- M1 original target baseline: `3748b33ffa37a0f977d9ba448e6d760a639b5eba`
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

## E-008.4 Issue Contract (M1 only) — CLOSED at `026190f`; closure patch BELOW
Audit-remediation of E-008.3 (verdict **Conditional Fail**). Baseline `b7012cb` is
kept intact; `026190f` closed three state-transition gaps (deprecated-version
idempotency P1-1, lease-bound `previous_active_version` P1-2, in-process
execution-attempt guard P1-3). A code-level re-review of `026190f` returned
**Conditional Fail** on two narrow points, addressed by the closure patch in this
commit (kept a strict subset of the E-008.4 allowed paths; no upstream
modifications, no `config.py`/`domain/` changes, `026190f` not rolled back):

- **P1-2 (upgrade path) — backfill in a NEW migration `008`.** The one-time
  `UPDATE document_builds SET previous_active_version = (SELECT
  ingestion_jobs.previous_active_version ...) WHERE previous_active_version IS NULL`
  lives in `008_e0084_lease_previous_version_backfill.sql`, **not** inside the
  already-published `006`. A database that already deployed `006` records it as
  applied, so the migrator skips a modified `006` and the backfill would never run
  on upgrade; placing it in `008` guarantees it executes for already-upgraded
  databases. Idempotent (only fills NULL rows). `006` remains ALTER-only
  (adds the nullable column). An E-008.3 DB upgraded in place thus copies the
  per-job replaced version onto the (already-claimed) lease it owns; without it, a
  post-commit-failed build taken over after upgrade recomputed the replaced version
  against the already-switched active version and failed to clean the true prior
  data plane.
- **P1-3 (cross-process) — DB-backed execution attempt.** `document_builds` gains
  `attempt_id TEXT` + `claimed_at TEXT` (`007_e0084_build_attempt.sql`). Each `run()`
  mints a fresh `attempt_id` (uuid); `acquire_job` persists it on every
  claim/takeover/resume. A same-`job_id` re-acquire while the lease is still
  `running` with a **different `attempt_id`** is a duplicate delivery (e.g. a second
  process re-delivering the same `job_id`), rejected with `BuildConflict` without
  advancing the fencing generation — closing the cross-process race. Explicit recovery
  (`run(recover=True)` / `DocumentManager.ingest(recover=True)`) advances the
  generation; a **terminal** same-`job_id` lease (build-lease status `'done'` for a
  succeeded job, or `'failed'`) resumes without `recover=True`. `mark_job_terminal`
  only synchronizes `SUCCEEDED`/`FAILED` to the build lease, so `CANCELLED` is NOT a
  build-lease terminal state. `recover=True` is a **force recovery** and must only be
  called once the prior attempt is known stopped (old worker dead/halted); Parent/Qdrant
  writes do not re-check ownership mid-step. The in-process guard (`_claim_build_guard`)
  is retained. Cross-process **liveness** (detecting a crashed attempt that never
  released the lease) still needs a lease timeout/heartbeat and is explicitly out of scope.

The three original P1 fixes (P1-1 deprecated idempotency; P1-2 lease-bound
`previous_active_version`; P1-3 in-process execution-attempt guard) are unchanged
from `026190f`.

### Migration (closure patch)
- `migrations/006_e0084_lease_previous_version.sql` — ALTER-only; adds the nullable
  `document_builds.previous_active_version` column (P1-2). **Unchanged in shape** from
  the prior closure commit so already-deployed DBs skip it.
- `migrations/007_e0084_build_attempt.sql` — NEW; `document_builds.attempt_id`,
  `document_builds.claimed_at` (P1-3 cross-process execution attempt).
- `migrations/008_e0084_lease_previous_version_backfill.sql` — NEW; one-time backfill
  of `previous_active_version` for already-upgraded DBs (P1-2 upgrade path).

### Acceptance tests (closure patch)
- `tests/unit/test_metadata_store.py` — `test_build_attempt_rejects_duplicate_execution_for_same_job_id`
  (P1-3 DB-level attempt: duplicate RUNNING same-`job_id` rejected; `recover=True`
  advances), `test_migration_008_backfills_on_real_upgrade_from_deployed_026190f`
  (P1-2 real upgrade path: migrator skips `006`, runs `008` backfill; post-upgrade
  takeover inherits the true replaced version), `test_acquire_resumes_terminal_succeeded_lease_without_recover`
  (P1-3 terminal `'done'` lease resumes without `recover=True`; no `JobStatus` crash).
- `tests/integration/test_e008_crash_points.py` — `test_deprecated_version_redelivery_is_idempotent`
  (P1-1), `test_takeover_after_publish_failure_keeps_true_previous_version` (P1-2),
  `test_same_job_id_concurrent_delivery_is_serialized` (P1-3 in-process),
  `test_upgrade_008_backfill_then_takeover_cleans_old_data_plane` (P1-2 full pipeline:
  real upgrade → backfill → takeover → publish cleans old data plane).
- `tests/baseline/` MUST remain green.
- `ruff`, `mypy src/agentic_rag_enterprise`, full `pytest` (incl. `tests/baseline/`) all green.

## E-019 Allowed Changes (M3 only)
- `src/agentic_rag_enterprise/judge/` (new): `models.py` (`FactStatus`, `RequiredFact`, `FactCoverage`, `CoverageJudgeResult`, `SufficiencyResult`, `GapRetrievalPlan`, `StopDecision`), `protocol.py` (`Judge`, `JudgeError`, `JudgeTimeoutError`), `deterministic_coverage_judge.py` (`DeterministicCoverageJudge`), `query_fact_extractor.py` (`DeterministicQueryFactExtractor`), `claim_evidence_verifier.py` (`DeterministicClaimEvidenceVerifier`).
- `src/agentic_rag_enterprise/answer/envelope.py` — add `coverage: SufficiencyResult | None`, `gap_rounds: int`; preserve the abstain/insufficient lock.
- `src/agentic_rag_enterprise/answer/verification.py` — extend `ClaimVerificationResult` with per-claim `support_status`.
- `src/agentic_rag_enterprise/answer/builder.py` — extend `build_answer_envelope(..., coverage=, claim_verification=)`.
- `src/agentic_rag_enterprise/services/chat_service.py` — add `answer_with_iteration`; `answer()` unchanged in behaviour.
- `tests/unit/judge/`, `tests/unit/test_chat_service_iteration.py`, `tests/integration/test_e019_e020_pipeline.py`.
- `docs/issue-e019-contract.md`, `docs/issue-e020-contract.md`, `AGENTS.md`.
- **Reuse, no change:** `retrieval/fast_path.py`, `retrieval/retriever.py`, `domain/evidence.py`, `domain/security.py`, `providers.py`, `config.py`.
- **Forbidden:** no `agents/`/`graph/` M0 runtime extension; no second retrieval pass (E-020); no real LLM judge; no change to E-011/E-012/E-013 behaviour beyond the agreed `AnswerEnvelope`/`verify_claims` extensions.

## E-020 Allowed Changes (M3 only)
- `src/agentic_rag_enterprise/judge/gap_planner.py` (`GapPlanner`), `src/agentic_rag_enterprise/judge/stop_policy.py` (`StopPolicy`) — reuse `models.py`/`protocol.py`/`deterministic_coverage_judge.py`.
- `src/agentic_rag_enterprise/services/chat_service.py` — `answer_with_iteration` bounded loop (reuses `retriever.retrieve_evidence(..., iteration=round)`); `answer()` unchanged.
- `src/agentic_rag_enterprise/answer/builder.py` — `coverage`/`claim_verification`/`gap_rounds`/`iterations`/`tool_calls`/`missing_aspects`.
- `src/agentic_rag_enterprise/evals/` — `dataset.py`, `runner.py`, `metrics.py` extend (`false_sufficient`, `judge_timeout_degradation`), `evals/data/m3_v1.json`.
- `tests/unit/judge/test_gap_planner.py`, `tests/unit/judge/test_stop_policy.py`, `tests/evals/test_evals_harness.py`.
- **Reuse, no change:** `retrieval/fast_path.py`, `retrieval/retriever.py`, `domain/evidence.py`, `domain/security.py`, `providers.py`, `config.py`.
- **Forbidden:** no `agents/`/`graph/` M0 runtime extension; no Planner/DAG, no multi-corpus, no unbounded loop (`max_rounds` default 3 + `no_new_evidence` are hard stops); faults never relabelled as answers; no real LLM judge.

## E-017 Allowed Changes (M5 only) — CLOSED / ACCEPTED at `398f059`
Typed `QueryPlan` / `PlanStep` contract + DAG Validator. **Pure control plane; no
execution, no `SecureRetriever` call, no Tool.** Full contract at
`docs/issue-e017-contract.md`.
- `src/agentic_rag_enterprise/planner/` (new): `models.py` (`PlanStep`, `QueryPlan`,
  `StepDependency`, `PlanViolation`, `PlanViolationCode`, `PlanValidationResult` — all
  frozen + validated; `PlanStep` adds `optional_depends_on_step_ids` for §13.2 optional
  deps), `binding.py` (`BindingExpression` + `parse` for the §13.2 grammar
  `steps.<step_id>.outputs.<field>` / `facts.<fact_id>.value`; `BindingSyntaxError`),
  `validator.py` (`PlanValidator.validate(plan, ctx, registry)` — step_id uniqueness,
  dependency existence, DAG cycle detection, corpus authorization via
  `registry.get(...)` fail-closed, capability allowlist via `CapabilityCatalog.supports`,
  static budget pre-validation — each step ≤ global **AND** `sum(steps) ≤ global` — query
  non-empty, `input_bindings` reference legal upstream steps/facts, no write operation),
  `repair.py` (`parse_plan` structured-output parse with **at most one** injected
  `repair_fn`; `PlanRepairExhaustedError`), `__init__.py`.
- `tests/unit/planner/` (new): `test_binding.py`, `test_plan_models.py`,
  `test_plan_validator.py`, `test_plan_repair.py`.
- `tests/integration/test_e017_planner_contract.py` (new): illegal DAG → zero Tool
  (structurally — no executor exists), cycle rejected, missing/undeclared binding
  rejected, unauthorized Corpus never in an accepted plan (name not leaked to user),
  total budget statically rejected, malformed planner output repaired at most once.
- `docs/issue-e017-contract.md`, `AGENTS.md`.
- **Reuse, no change:** `judge/models.py` (`RequiredFact`), `corpus/registry.py`,
  `corpus/capability_registry.py`, `domain/security.py`, `retrieval/models.py`
  (`CorpusNotDiscoverableError`).
- **Forbidden:** no Executor / `StepResult` / scheduling / step timeout / retry / budget
  allocator (all E-018); no real `SecureRetriever` / Tool call; no LLM Planner (repair_fn
  is injected); no temporal-conflict arbitration; no `agents/`/`graph/` runtime change; no
  change to E-011→E-016 behaviour.

## E-018 Allowed Changes (M5 only) — contract amended (P1-1..P1-5 + P2 + 6-item re-audit fix) / implementation pending
Controlled Executor + dependent multi-hop (build plan §13.4). Full contract at
`docs/issue-e018-contract.md`.
- `src/agentic_rag_enterprise/planner/` (extend): `executor.py` (`PlanExecutor.execute(
  plan, ctx, registry, *, tool_registry, concurrency=...)` — re-validates the plan via
  `PlanValidator.validate`, then runs topological layers with `AtomicToolBudget`),
  `result.py` (`StepResult`, `StepStatus`, `PlanExecutionResult` — all frozen + validated;
  `StepStatus` = pending/running/succeeded/failed/timed_out/skipped_dependency/
  budget_exhausted; `StepResult.detail` `Field(exclude=True, repr=False)`;
  `PlanExecutionResult` carries `degraded`, `limitations`, `tool_calls_used` (= budget used),
  `evidence_ids` (first-occurrence deterministic dedup), `detail` `exclude=True, repr=False`;
  **no** whole-execution `error_code`/`message` (fail-closed → `PlanExecutionError`);
  "usable result" = ≥1 succeeded step with Evidence, else raise `PlanExecutionError`),
  `budget.py` (`AtomicToolBudget` — single `try_reserve(n)` API doing `remaining -= n;
  used += n` atomically; no separate `reserve`+`consume`; no refund;
  `PlanStep.max_tool_calls` is runtime cap; multi-corpus step reserves N units per attempt),
  `tool_registry.py` (`ToolRegistry.get()` returns `(Tool, ToolSpec)` with ToolSpec having
  `input_model` (field requiredness via `is_required()` for missingness decision) +
  `output_models: Mapping[OutputSchemaId, type[BaseModel]]` covering all four schema IDs +
  `retryable_errors`) + `Tool` protocol; `RetrieverTool` wrapping `SecureRetriever.
  retrieve_evidence` and deterministically projecting `list[SnapshotEvidence]` →
  `entity_text`/`spec_text` per `output_schema_id` (no LLM; sort: `retrieval_score` desc
  (None→0.0), `authority_level` desc, `evidence_id` asc; empty list → empty outputs);
  lookup by `step_type + capability_id`),
  `errors.py` (`PlanExecutionError`).
- `tests/unit/planner/test_executor.py`, `tests/unit/planner/test_atomic_budget.py`,
  `tests/integration/test_e018_executor_pipeline.py` — cover the §11 acceptance matrix
  (parallel, diamond-once, binding, required-skip, optional-continue, timeout-no-overwrite,
  retry-once-consumes-2, no-retry-on-programming-error, budget=1 single-flight,
  retry+parallel no-overspend, unauthorized fail-closed, security-no-degrade, illegal-zero-
  tool, deterministic order, tool_calls_used matches, no corpus/tenant leak, no dynamic step;
  plus the 6-item re-audit: max_tool_calls runtime cap, multi-corpus budget, ToolRegistry
  return type + output_models mapping, is_required() missingness, projection edge cases,
  PlanExecutionResult error removal + evidence_ids first-occurrence dedup).
- `docs/issue-e018-contract.md`, `AGENTS.md`.
- **Reuse, no change:** `planner/models.py` (`QueryPlan`/`PlanStep`/`StepDependency`),
  `planner/binding.py` (grammar parse), `planner/validator.py` (`PlanValidator`),
  `corpus/registry.py`, `corpus/capability_registry.py`, `domain/security.py`,
  `retrieval/retriever.py` (`SecureRetriever.retrieve_evidence`), `retrieval/models.py`
  (`CorpusNotDiscoverableError`, `RetrievalBackendError`), `retrieval/backend_fault.py`.
- **Forbidden:** no change to E-017 `QueryPlan` semantics (raise as contract amendment if a
  hard gap appears); no temporal/authority/conflict arbitration; no distributed scheduling;
  no infinite repair/retry; no Planner/Tool reading client-supplied tenant/role (only the
  Executor injects `SecurityContext`); no write operation (`sql`/`api`/`graph`); no dynamic
  step creation; no change to E-011→E-016 behaviour.

## E-021 Allowed Changes (M6 only) — CLOSED / ACCEPTED at `c6c3e6b`
Temporal scope, source authority & conflict (build plan §15 / Milestone 6). Implemented and
accepted; the three P1 main-path fixes (`c6c3e6b`) are within this scope. Planner core stayed
frozen. Full contract at `docs/issue-e021-contract.md`.

- `docs/issue-e021-contract.md`, `AGENTS.md`.
- **This commit is contract-only (amendment)**: it amends the `30c1d2e` freeze to fix three
  main-path-breaking defects — (1) `as_of` vs `range` parser precedence (explicit range →
  `as_of` → `current` → bare-year range → `unspecified`; bare-year rule suppressed when an
  `as_of` marker is present); (2) `ConflictReport` no longer carries `OverallStatus` — it emits
  a resolver-only `ConflictStatus` (`NONE` / `RESOLVED` / `CONTRADICTED`), and only
  `CONTRADICTED` forces `SufficiencyResult.overall_status = "contradicted"` (NONE / RESOLVED
  leave the judge untouched, preserving M2–M5 behavior); (3) conflict detection now requires a
  structured `assertion` parser (version / boolean / quantity / key:value) — differing full
  text alone NEVER creates a candidate, so normal complementary multi-evidence answers are not
  mis-flagged. `TemporalScope`, the deterministic temporal parser + filter, the
  `ConflictResolver` (five conflict types, four explicit auto-resolution rules), the
  `ConflictReport` model, and the minimal `AnswerEnvelope.conflict_report` extension are all
  frozen. Implementation opens **after** acceptance.
- **Reuse, no change:** `domain/evidence.py` (`Evidence` snapshot — all resolver fields already
  present), `retrieval/retriever.py` (`SecureRetriever.retrieve_evidence` — authorized
  collection only), `judge/models.py` (`SufficiencyResult`, `OverallStatus`, `FactCoverage`),
  `answer/envelope.py` (`AnswerEnvelope`), `services/chat_service.py` (integration call
  sites), `retrieval/fast_path.py`, `corpus/registry.py`, `domain/security.py`.
- **Forbidden:** no change to `QueryPlan` / `PlanStep` / `executor.py` / `result.py` /
  `budget.py` / `tool_registry.py` (Planner core stays frozen); no LLM time-reasoning / value-
  extraction / NER in the resolver; no new `Evidence` field; no reliance on `retrieval_score` /
  `rerank_score` to pick a winning fact; no change to E-011→E-020 behaviour beyond the agreed
  `AnswerEnvelope.conflict_report` extension; no upstream modification.

## E-022 Allowed Changes (M7 only) — CLOSED / ACCEPTED at `cd4ddb2`
Runtime hardening (build plan Milestone 7): Reconciler + purge + index migration + rollback
(`E-022`). Full contract at `docs/issue-e022-contract.md`. In scope: standalone `Reconciler`
(deterministic, idempotent, fenceable; metadata-DB-as-truth; orphan purge, missing-data-plane
rebuild, post-commit cleanup retry per §10.10 #6, dead-letter cleanup, registry realign,
dry-run + audit table); index migration (new `corpus_id_v{emb}_{chunk}` collection built
alongside v1, atomic registry-pointer switch, retain v1); index + active-version rollback;
reconciler-driven purge hardening. Depends on E-008.x / E-010 / E-011 / E-015 / E-016.
- **Reuse, no change:** `domain/ingestion.py`, `domain/document.py`, `domain/chunk.py`,
  existing `MetadataStore` lease/query primitives, `VectorStore.delete`/`update_payload`/
  `list_point_ids_by_document`/`search`, `ParentStore` delete/deprecate,
  `DocumentManager.delete`/`purge`/`_compensate`.
- **Forbidden:** no LLM/NLP in reconciler or index migration (deterministic, hermetic); no new
  model download / external API in tests; no "reserved interface" for E-023 (checkpoint/
  re-auth) or E-024 (readiness/cancellation/backup/restore) — those stay in their issues; no
  Postgres / Qdrant Server / SSO (M9); no clear-and-rebuild of the production collection
  (§10.8); reconciler must never resurrect deleted/purged evidence; no change to Planner core
  (`planner/`, `executor.py`, `result.py`, `budget.py`, `tool_registry.py`). E-023 / E-024
  overlap explicitly deferred.

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
