# E-019 Issue Contract (M3) — Required-Fact Coverage judge + explicit state transitions

First half of Milestone 3 (single-corpus quality iteration, build plan §3589 /
§7.7 / §7.8 / §14.1–§14.3 / §14.7). Adds the **Stage A Evidence Coverage Judge**
and the **Stage B Claim-Evidence Verifier** to the existing run-chain, so the
service can report *what is missing* and drive explicit, fail-closed state
transitions (`sufficient` → complete, `partially_sufficient` → partial + missing
list, `insufficient`/`contradicted` → abstain) instead of the boolean
sufficient/insufficient the E-012 Fast Path alone produces.

E-019 does **not** perform multi-round retrieval (that is E-020). It judges the
Evidence already retrieved by the Fast Path and exposes the per-fact coverage on
the `AnswerEnvelope`. The judge is **deterministic / heuristic** for the Internal
MVP (no real LLM), behind a pluggable `Judge` protocol so an LLM judge can be
swapped in later (E-013 defers multi-model semantic entailment & Judge
calibration; E-020 reuses the same protocol).

## depends_on
- **E-012** — `run_fast_path`, `FastPathResult`, `FastPathSufficiency`, `FastPathBackendError`. E-019 reads the `evidence` the Fast Path returned and must not relabel a `FastPathBackendError` into a coverage verdict.
- **E-011** — `domain.evidence.Evidence` (the snapshots judged for coverage).
- **E-013** — `AnswerEnvelope`, `Claim`, `build_answer_envelope`, `conservative_refusal`, `verify_claims`. E-019 extends `AnswerEnvelope` with a `coverage` field and extends `verify_claims` with per-claim `support_status`; the E-013 abstain/insufficient lock must be preserved.
- **E-014** — `ChatService.answer` is the integration point; E-019 adds `answer_with_iteration` (single-pass for E-019) without changing `answer()`'s behaviour.
- **`providers.py`** — `ModelProvider` protocol (the deterministic judge mirrors this shape via a `Judge` protocol).

## in_scope
- **New `judge/` package** (build plan §28.2 forbids a second runtime, so this is a
  plain package, NOT the legacy `agents/`/`graph/` M0 mock runtime):
  - `judge/models.py` — `FactStatus` (enum of the 7 statuses, §7.7/§14.3),
    `RequiredFact` (§7.7), `FactCoverage` (§7.7), `CoverageJudgeResult`,
    `SufficiencyResult` (§7.8), `GapRetrievalPlan` (§14.4, shared with E-020),
    `StopDecision` (§14.5, shared with E-020). All frozen pydantic.
  - `judge/protocol.py` — `Judge` Protocol (`judge(*, query, required_facts, evidence, timeout=None) -> CoverageJudgeResult`, `name` attr), `JudgeError`, `JudgeTimeoutError`.
  - `judge/deterministic_coverage_judge.py` — `DeterministicCoverageJudge` (Stage A): per `RequiredFact`, lexical token overlap vs `Evidence.text`; negation/antonym ⇒ `contradicted`; partial ⇒ `partially_supported`; absent ⇒ `missing`; empty evidence ⇒ `not_retrievable`. Computes `overall` by the fixed priority `policy_blocked > ambiguous > contradicted > sufficient > partially_sufficient > insufficient` (§14.3). Implements `Judge`.
  - `judge/query_fact_extractor.py` — `DeterministicQueryFactExtractor` (wh-/keyword decomposition → candidate `RequiredFact`s); also accepts facts supplied directly (eval dataset / request).
  - `judge/claim_evidence_verifier.py` — `DeterministicClaimEvidenceVerifier` (Stage B): each kept `Claim` gets a `support_status` (entailed/partially_entailed/contradicted/unsupported) by overlap/negation vs its cited `Evidence`.
- **`answer/envelope.py`** — add `coverage: SufficiencyResult | None = None` and `gap_rounds: int = 1` to `AnswerEnvelope`. The existing `completeness` (`complete`/`partial`/`insufficient`/`conflicted`), `confidence` (`high`/`medium`/`low`), `iterations`/`tool_calls`, and the abstain/insufficient lock are **preserved**; `_lock_state` is unchanged except it ignores `coverage`.
- **`answer/verification.py`** — extend `ClaimVerificationResult` with the per-claim `support_status` list (or annotate kept claims); `verify_claims` keeps its dangling-citation check.
- **`answer/builder.py`** — `build_answer_envelope(..., coverage=None, claim_verification=None)` populates the new fields; `completeness`/`confidence` mapping honours §14.7 (`sufficient`→complete/high, `partially_sufficient`→partial/medium, `insufficient`→insufficient/low/abstain, `contradicted`→conflicted/low, `ambiguous`→partial/low). `conservative_refusal` keeps `gap_rounds=1`.
- **`services/chat_service.py`** — refactor `answer()` internals into `_run_single_pass`; add `answer_with_iteration(max_rounds=3, judge=None, required_facts=None)`; `answer()` delegates to `answer_with_iteration(max_rounds=1, judge=None)` so E-014 behaviour is unchanged. For E-019 the judge runs once after the Fast Path; the `SufficiencyResult` is attached to the envelope.
- **`tests/unit/judge/*`** and **`tests/unit/test_chat_service_iteration.py`** and **`tests/integration/test_e019_e020_pipeline.py`** (shared with E-020).
- **`docs/issue-e019-contract.md`** (this file) + **`AGENTS.md`** update.

## deferred_to
- **E-020** — multi-round / gap-driven retrieval loop, `GapPlanner`, `StopPolicy` (E-019 is single-pass judging).
- **LLM Judge** — semantic-entailment / calibrated judge (E-013 defers Judge calibration; the `Judge` protocol is the seam).
- **Multi-corpus** (M4/E-015/E-016), **Planner/DAG** (M5/E-017/E-018), **temporal/conflict** (M6/E-021), **formal eval/red-team** (M8/E-025–E-027).
- **Real LLM providers** beyond `provider="fake"` (only the protocol + `FakeModel` exist).

## allowed_paths (M3 only)
- `src/agentic_rag_enterprise/judge/` (new package: `models.py`, `protocol.py`, `deterministic_coverage_judge.py`, `query_fact_extractor.py`, `claim_evidence_verifier.py`).
- `src/agentic_rag_enterprise/answer/envelope.py` (extend, preserve locks).
- `src/agentic_rag_enterprise/answer/verification.py` (extend).
- `src/agentic_rag_enterprise/answer/builder.py` (extend signature).
- `src/agentic_rag_enterprise/services/chat_service.py` (add `answer_with_iteration`; `answer()` unchanged in behaviour).
- `tests/unit/judge/`, `tests/unit/test_chat_service_iteration.py`, `tests/integration/test_e019_e020_pipeline.py`, `tests/evals/test_evals_harness.py`.
- `docs/issue-e019-contract.md`, `docs/issue-e020-contract.md`, `AGENTS.md`.
- **Reuse, no change:** `retrieval/fast_path.py`, `retrieval/retriever.py`, `domain/evidence.py`, `domain/security.py`, `providers.py`, `config.py`.

## forbidden
- No extension of the legacy `agents/` (`planner.py`, `sufficient_context.py`, `synthesis.py`) or `graph/` (`runtime.py`, `state.py`) M0 mock runtime (§28.2).
- No multi-corpus, no Planner/DAG, no second retrieval pass (that is E-020).
- No modification of E-011/E-012/E-013 behaviour or types beyond the agreed `AnswerEnvelope`/`verify_claims` extensions; the abstain ⇒ `stop_reason == no_evidence` lock must hold.
- No masking of faults as answers/refusals: `FastPathBackendError` and model errors propagate (→ 5xx); a retrieval outage is never relabelled as `insufficient` coverage.
- No real LLM in the judge for the Internal MVP; the judge must be deterministic and network-free.
- No upstream modifications; no reserved/placeholder modules or runtime branches not exercised by the E-019 tests.

## acceptance_tests
- `tests/unit/judge/test_models.py` — `FactStatus` priority ordering matches §14.3; `SufficiencyResult` overall derivation.
- `tests/unit/judge/test_deterministic_coverage_judge.py` — supported / partially_supported / missing / contradicted / not_retrievable per-fact verdicts; `overall` by fixed priority; empty evidence ⇒ `not_retrievable`.
- `tests/unit/judge/test_query_fact_extractor.py` — wh-/keyword decomposition yields candidate `RequiredFact`s; supplied facts override.
- `tests/unit/judge/test_claim_evidence_verifier.py` — each kept claim gets a `support_status`; contradicted/unsupported detected by overlap/negation.
- `tests/unit/test_chat_service_iteration.py` — `answer()` stays single-pass (E-014 green); `answer_with_iteration(max_rounds=1)` attaches `coverage` to the envelope; `sufficient`→complete, `partially_sufficient`→partial with missing list, `contradicted`→conflicted, `insufficient`→abstain.
- `tests/integration/test_e019_e020_pipeline.py` — fake retriever + `DeterministicCoverageJudge` end-to-end; `coverage` populated; `AnswerEnvelope` passes `_lock_state`.
- Regression that MUST stay green: E-011/12/13/14 unit + integration, `tests/unit/test_retrieval_boundary.py`, `tests/baseline/`.
- Quality gates: `ruff check`, `ruff format --check` (whole tree), `mypy src/agentic_rag_enterprise`, `git diff --check` all clean.

## acceptance_commands
```bash
.venv/bin/python -m pytest tests/unit/judge tests/unit/test_chat_service_iteration.py tests/integration/test_e019_e020_pipeline.py -q
.venv/bin/python -m pytest tests/unit tests/integration/test_e011_evidence_pipeline.py tests/unit/test_retrieval_boundary.py tests/baseline -q
.venv/bin/python -m ruff check src/agentic_rag_enterprise tests
.venv/bin/python -m ruff format --check src/agentic_rag_enterprise tests
.venv/bin/python -m mypy src/agentic_rag_enterprise
git diff --check
```
