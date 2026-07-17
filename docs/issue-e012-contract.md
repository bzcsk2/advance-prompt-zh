# E-012 Issue Contract (M2) — Single-corpus Fast Path and one-pass sufficiency decision

Second capability of Milestone 2 (single-corpus Internal MVP, build plan
§3576 / §5.2 / §14.3). Sits directly on top of E-011's
`SecureRetriever.retrieve_evidence()`, which already performs the secure flow
(corpus-discoverability gate → authorized hybrid child retrieval → parent
second-authorization → deduplication → immutable Evidence snapshot). E-012 does
**not** re-implement retrieval, deduplication, ACL, snapshot, or re-authorization
— it adds the *one-pass Fast Path decision* that turns a single evidence
retrieval into a typed `sufficient` / `insufficient` result the answer phase
(E-013) consumes.

Baseline for this issue is the current working tree (E-011 acceptance-remediation
change set is in flight and MUST be preserved intact — see AGENTS.md). E-012 is
strictly additive: it does not modify `retrieve_evidence`, the deduplicator, the
evidence store, or any ACL/snapshot behaviour.

## depends_on
- **E-011** — `SecureRetriever.retrieve_evidence(ctx, query, corpus, ...)`
  returns `list[domain.evidence.Evidence]` (the immutable M2 snapshot). E-012
  reuses it verbatim as its single retrieval dependency and reuses its
  corpus-discoverability gate, parent second-authorization, three-way
  deduplication, and immutable snapshot semantics.
- **E-007 / E-009** — the secure retrieval boundary (corpus discoverability +
  parent second-auth) that `retrieve_evidence` already enforces. E-012 passes
  `SecurityContext` and `CorpusConfig` *unchanged* straight into that boundary.
- **build plan §5.2** — Fast Path: no Planner DAG, at most one retrieval pass.
- **build plan §14.3 / §14.7** — deterministic baseline sufficiency:
  evidence present → `sufficient` (full answer in later phase); no evidence →
  `insufficient` (downstream must conservatively abstain / refuse).

## in_scope
- Single-tenant, single-Corpus Fast Path. Accepts (`SecurityContext`, `query`,
  one already-authorized `CorpusConfig`) plus a `SecureRetriever`.
- Exactly **one** call to `SecureRetriever.retrieve_evidence()` per request —
  no second retrieval, no multi-round loop, no gap planner.
- Deterministic baseline sufficiency rule (no LLM judge):
  - ≥1 `Evidence` returned → `sufficient`.
  - 0 `Evidence` returned → `insufficient`, signalling the answer phase to
    conservatively refuse.
- Typed `FastPathResult` carrying: the `Evidence` list, `sufficiency` status,
  and a `stop_reason`. Derived `is_sufficient` and `should_abstain` booleans
  encode the §14.7 mapping for the downstream consumer.
- Typed `FastPathBackendError` so a retrieval/infra fault is **never** misread as
  an `insufficient` (no-answer) decision; the underlying cause is preserved.
- E-011 Evidence Snapshot, ACL, deduplication, and read-authorization behaviour
  are used but left unchanged.

## deferred_to
- **E-013** — `AnswerEnvelope`, grounded answer generation, citation rendering,
  single key-claim support verification, and the actual conservative-refusal
  wording. E-012 only *signals* `should_abstain`; it produces no answer text.
- **E-014** — FastAPI `/v1/chat`, Gradio adapter, and the shared chat
  application service that will call `run_fast_path`.
- **E-019 / E-020** — Required Fact LLM Judge, `GapRetriever`/`Gap Planner`,
  multi-round retrieval, and the `no-new-evidence` stop loop. E-012's
  sufficiency is the deterministic baseline only.
- **Milestone 4** — multi-Corpus, Corpus Router, cross-Corpus merge. E-012 is
  single-Corpus only.
- **Milestone 5** — Planner, Typed DAG, Executor, dependency multi-hop.
- Reranker, temporal/version-conflict handling, persistent Checkpoint, SSE.
- A second retrieval call, a second Evidence store, or any Graph Runtime.

## allowed_paths (M2 only)
- `src/agentic_rag_enterprise/retrieval/fast_path.py` (new) —
  `run_fast_path`, `FastPathResult`, `FastPathSufficiency`,
  `FastPathStopReason`, `FastPathBackendError`.
- `src/agentic_rag_enterprise/retrieval/__init__.py` — export the new symbols
  (`run_fast_path`, `FastPathResult`, `FastPathSufficiency`,
  `FastPathStopReason`, `FastPathBackendError`).
- `tests/unit/test_fast_path.py` (new) — focused unit tests (hermetic fake
  `SecureRetriever`, no Qdrant).
- `docs/issue-e012-contract.md` (this file).
- `AGENTS.md` — update Current Milestone & Issue.
- **Reuse, no change:** `retrieval/retriever.py` (`SecureRetriever`,
  `retrieve_evidence`), `retrieval/deduplication.py`, `storage/evidence_store.py`
  (and its read-time re-authorization), `security/filter.py`, `domain/evidence.py`,
  `domain/security.py`, `domain/corpus.py`, `storage/vector_store.py`
  (`DenseEncoder`/`SparseEncoder`).

## forbidden
- No AnswerEnvelope / answer generation / Claim Verification / Citation
  Rendering (E-013).
- No FastAPI / Gradio / application service (E-014).
- No Required Fact LLM Judge / Gap Planner / multi-round retrieval /
  `no-new-evidence` loop (E-019/E-020).
- No multi-Corpus / Corpus Router / cross-Corpus merge.
- No Planner / Typed DAG / Executor / dependency multi-hop. The Fast Path must
  not import `agents/planner.py` or call any planner.
- No Reranker / temporal conflict / persistent Checkpoint / SSE.
- No second retrieval, second Evidence store, or Graph Runtime.
- No modification of E-011 behaviour: `retrieve_evidence`, the deduplicator,
  the evidence store, ACL handling, or snapshot immutability.
- No reserved/placeholder modules, DB tables, or runtime branches not exercised
  by the E-012 tests.
- No upstream modifications; no `config.py`/`domain/` changes beyond reuse.

## Acceptance tests
- `tests/unit/test_fast_path.py` —
  - `test_sufficient_when_evidence_present`: ≥1 `Evidence` → `sufficient`,
    `stop_reason == evidence_found`, `should_abstain is False`.
  - `test_insufficient_when_no_evidence`: empty list → `insufficient`,
    `stop_reason == no_evidence`, `should_abstain is True`.
  - `test_exactly_one_retrieve_evidence_call`: spy asserts
    `retrieve_evidence` called exactly once.
  - `test_context_and_corpus_passed_unchanged`: the same `SecurityContext`
    instance and the same `CorpusConfig` instance (identity, not a copy) reach
    `retrieve_evidence`; `query` is forwarded verbatim.
  - `test_no_planner_no_second_round`: only `retrieve_evidence` is invoked once;
    no planner import/call and no second query is produced.
  - `test_retrieval_error_propagates_as_backend_error`: a raised retrieval
    dependency error surfaces as `FastPathBackendError` (not an `insufficient`
    result); the original cause is preserved.
- Regression that MUST stay green (E-011 / baseline / boundary):
  - `tests/unit/test_deduplication.py`, `tests/unit/test_evidence_store.py`,
    `tests/integration/test_e011_evidence_pipeline.py`
  - `tests/unit/test_retrieval_boundary.py`, `tests/baseline/`
- Quality gates:
  - `ruff check src/agentic_rag_enterprise tests` clean.
  - `mypy src/agentic_rag_enterprise` clean.
  - `git diff --check` clean.

## Acceptance commands
```bash
# E-012 focused unit suite (run tonight)
.venv/bin/python -m pytest tests/unit/test_fast_path.py -q

# Must remain green (no regression of E-011 / boundary / baseline)
.venv/bin/python -m pytest tests/unit -q

# Quality gates (run tonight)
.venv/bin/ruff check src/agentic_rag_enterprise tests
.venv/bin/mypy src/agentic_rag_enterprise
git diff --check
```
