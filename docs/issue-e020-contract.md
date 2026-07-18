# E-020 Issue Contract (M3) — Bounded gap retrieval + no-new-evidence stop policy

Second half of Milestone 3 (single-corpus quality iteration, build plan §3589 /
§14.4–§14.6 / §5064–§5065). Reuses the E-019 Coverage Judge and adds the
**Gap Planner** and **Stop Policy** so the service can perform a *bounded*
(2–3 round) gap-driven retrieval loop that stops on `no_new_evidence`, `max_rounds`,
or budget/timeout exhaustion — never an unbounded loop. Single-corpus only;
no Planner/DAG, no multi-corpus (those are M4/M5).

The loop is driven by `ChatService.answer_with_iteration`, which reuses
`SecureRetriever.retrieve_evidence(..., iteration=round)` to accumulate Evidence
across rounds, re-judges coverage with the E-019 `Judge`, and only then synthesizes
the answer. `answer()` remains the single-pass E-014 path.

## depends_on
- **E-019** — `Judge` protocol, `DeterministicCoverageJudge`, `CoverageJudgeResult`, `SufficiencyResult`, `FactStatus`, `RequiredFact`, `FactCoverage`, `GapRetrievalPlan`, `StopDecision`, and `ChatService.answer_with_iteration` (single-pass).
- **E-012** — `run_fast_path`, `retrieve_evidence` (already supports `iteration=` / `plan_step_id=`), `FastPathBackendError`.
- **E-011 / E-013 / E-014** — Evidence snapshots, `AnswerEnvelope`, `build_answer_envelope`, `ChatService`.

## in_scope
- **`judge/gap_planner.py`** — `GapPlanner.plan(coverage: CoverageJudgeResult, *, prior_queries: list[str]) -> GapRetrievalPlan` (§14.4): emits sub-queries ONLY for `missing` / `partially_supported` facts (never for already-supported facts), each containing the missing fact text + known entities + the prior queries (no repeats), target corpus = the answered corpus.
- **`judge/stop_policy.py`** — `StopPolicy` with `decide(*, round, max_rounds, seen_evidence_ids, new_evidence_ids, new_covered_fact_ids, judge_ok, budget_remaining) -> StopDecision` (§14.5): stops on `sufficient` (all required facts supported), `no_new_evidence` (two consecutive rounds add no new Evidence id AND no new covered fact — §14.6 spirit), `max_rounds` (default 3), `budget_exhausted` / `tool_unavailable` (Judge timeout/timeout degradation). `StopDecision` carries `should_stop` + `reason`.
- **`services/chat_service.py`** — `answer_with_iteration(max_rounds=3, judge=None, required_facts=None)` loop:
  1. round 0 = `run_fast_path` → if `insufficient` → `conservative_refusal` (abstain). If `FastPathBackendError` → propagate.
  2. Stage A: `judge.judge(query, required_facts, evidence)` → `CoverageJudgeResult`.
  3. if `overall == sufficient` → synthesize (Stage B + `build_answer_envelope`).
  4. else if `StopPolicy` allows another round → `GapPlanner.plan(...)` → for each sub-query `retriever.retrieve_evidence(ctx, sub_query, corpus, top_k, iteration=round, ...)` (accumulate Evidence) → re-judge → repeat.
  5. after loop (exhausted or `partially_sufficient`): Stage B `DeterministicClaimEvidenceVerifier` then `build_answer_envelope(..., coverage=..., claim_verification=...)`, `gap_rounds = rounds_used`, `iterations = rounds_used`, `tool_calls = retrieval_calls`. On `contradicted`/`insufficient` final → `conservative_refusal` (abstain) with the coverage attached (abstain lock preserved).
- **`answer/builder.py`** — accept `coverage` + `claim_verification`; set `gap_rounds`/`iterations`/`tool_calls`; map `SufficiencyResult.overall` to `completeness`/`confidence` per §14.7; populate `missing_aspects` from `missing_fact_ids` descriptions when partial/abstained.
- **`evals/`** — `dataset.py` (versioned JSON `evals/data/m3_v1.json`), `runner.py` (drives `answer_with_iteration` with a fake retriever + `DeterministicCoverageJudge`), `metrics.py` extended with `false_sufficient(envelope, gold_missing) -> EvalResult` (answer `completeness == complete` but a required fact ∈ {missing, contradicted}) and `judge_timeout_degradation(...)` (asserts the answer degrades conservatively when the judge raises `JudgeTimeoutError`). Deterministic, no network.
- **`tests/evals/test_evals_harness.py`** — dataset load, `false_sufficient`, timeout-degradation (§14.5 scenario), loop-stop (§14.5 scenario 18 "循环无信息增益").
- **`docs/issue-e020-contract.md`** (this file) + **`AGENTS.md`** update.

## deferred_to
- **LLM Judge** — semantic / calibrated judging (the `Judge` protocol is the seam).
- **Multi-corpus** (M4/E-015/E-016), **Planner/DAG** (M5/E-017/E-018), **temporal/conflict** (M6/E-021), **formal eval/red-team + release profile** (M8/E-025–E-027).
- Real cost/token budgeting (M3 uses simple counters/exceptions; no real `MAX_QUERY_COST_USD` tracking).
- Regeneration loops / auto-rewrite of the answer from judge feedback (E-013 defers these).

## allowed_paths (M3 only)
- `src/agentic_rag_enterprise/judge/` (extend with `gap_planner.py`, `stop_policy.py`; reuse `models.py`, `protocol.py`, `deterministic_coverage_judge.py`).
- `src/agentic_rag_enterprise/services/chat_service.py` (`answer_with_iteration` loop; `answer()` unchanged).
- `src/agentic_rag_enterprise/answer/builder.py` (coverage/claim_verification/gap_rounds).
- `src/agentic_rag_enterprise/evals/` (`dataset.py`, `runner.py`, `metrics.py` extend).
- `tests/unit/judge/test_gap_planner.py`, `tests/unit/judge/test_stop_policy.py`, `tests/unit/test_chat_service_iteration.py`, `tests/integration/test_e019_e020_pipeline.py`, `tests/evals/test_evals_harness.py`, `evals/data/m3_v1.json`.
- `docs/issue-e019-contract.md`, `docs/issue-e020-contract.md`, `AGENTS.md`.
- **Reuse, no change:** `retrieval/fast_path.py`, `retrieval/retriever.py`, `domain/evidence.py`, `domain/security.py`, `providers.py`, `config.py`.

## forbidden
- No extension of the legacy `agents/` / `graph/` M0 mock runtime (§28.2).
- No Planner/DAG, no multi-corpus, no multi-hop, no reranker (M4/M5).
- No unbounded loop — `max_rounds` (default 3) and `no_new_evidence` are hard stops.
- No masking of faults: `FastPathBackendError`, retriever errors, and `JudgeTimeoutError` must never be relabelled as a grounded answer; on judge timeout the answer degrades conservatively (lower confidence / abstain) and the error is logged, not hidden.
- No real LLM in the judge for the Internal MVP.
- No change to E-011/E-012/E-013/E-014 behaviour beyond the agreed `AnswerEnvelope` extension; the abstain ⇒ `stop_reason == no_evidence` lock must hold (a coverage-attached abstain still sets `stop_reason == no_evidence`).
- No upstream modifications; no reserved/placeholder modules or runtime branches not exercised by the E-020 tests.

## acceptance_tests
- `tests/unit/judge/test_gap_planner.py` — queries only for `missing`/`partially_supported`; repeats of prior queries excluded; target corpus set.
- `tests/unit/judge/test_stop_policy.py` — stop on `sufficient`, `no_new_evidence` (two consecutive rounds), `max_rounds`, budget/timeout; continue when a round adds new evidence / a newly covered fact.
- `tests/unit/test_chat_service_iteration.py` — bounded loop honours `max_rounds`; `no_new_evidence` stops early; exhausted gaps → abstain (with `coverage`); `answer()` still single-pass green; `FastPathBackendError` propagates; `JudgeTimeoutError` degrades conservatively (not a fabricated answer).
- `tests/integration/test_e019_e020_pipeline.py` — fake retriever returns new evidence on gap rounds until coverage is `sufficient`; final envelope has `gap_rounds > 1` and populated `coverage`.
- `tests/evals/test_evals_harness.py` — dataset loads; `false_sufficient` fires when a `complete` envelope hides a missing required fact; judge-timeout degradation path.
- Regression that MUST stay green: E-011/12/13/14 unit + integration, `tests/unit/test_retrieval_boundary.py`, `tests/baseline/`.
- Quality gates: `ruff check`, `ruff format --check` (whole tree), `mypy src/agentic_rag_enterprise`, `git diff --check` all clean.

## acceptance_commands
```bash
.venv/bin/python -m pytest tests/unit/judge tests/unit/test_chat_service_iteration.py tests/integration/test_e019_e020_pipeline.py tests/evals -q
.venv/bin/python -m pytest tests/unit tests/integration/test_e011_evidence_pipeline.py tests/unit/test_retrieval_boundary.py tests/baseline -q
.venv/bin/python -m ruff check src/agentic_rag_enterprise tests
.venv/bin/python -m ruff format --check src/agentic_rag_enterprise tests
.venv/bin/python -m mypy src/agentic_rag_enterprise
git diff --check
```
