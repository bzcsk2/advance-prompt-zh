# E-013 Issue Contract (M2) — AnswerEnvelope, citation rendering, key-claim verification, conservative refusal

Third capability of Milestone 2 (single-corpus Internal MVP, build plan
§3576 / §7.9 / §16). Sits directly on top of:

* **E-012** `FastPathResult` (`retrieval/fast_path.py`) — the one-pass
  sufficient / insufficient decision carrying the retrieved `Evidence` and the
  `should_abstain` signal.
* **E-011** `domain.evidence.Evidence` — the immutable M2 snapshot used as the
  sole citation source (build plan §16.6: citations must be immutable snapshot
  refs, never a live link to the latest source document).

E-013 does NOT generate the natural-language answer (that is the LLM synthesis
the E-014 application service wires). It wraps a caller-supplied
`answer_markdown` + `claims` into a typed, validated `AnswerEnvelope`, renders
citations that resolve back to the Evidence Snapshots, runs the single
deterministic key-claim support check, and produces a conservative refusal when
the Fast Path says `insufficient`. Answer generation (LLM) and advanced
claim decomposition / Judge calibration / auto-rewrite are explicitly deferred.

## depends_on
- **E-012** — `FastPathResult` (`sufficiency`, `stop_reason`, `evidence`,
  `should_abstain`). E-013's builder consumes it directly; the abstain branch
  is driven by `FastPathResult.should_abstain` / `sufficiency`.
- **E-011** — `domain.evidence.Evidence` (immutable snapshot). Citations are
  rendered from these snapshots only; their `document_version`, `text_hash`,
  `retrieved_at`, `policy_version` are carried into the citation for the
  immutable-reference requirement (§16.6).
- **build plan §7.9** — `AnswerEnvelope` / `Claim` schema.
- **build plan §16.2** — synthesis prompt principles (no external-knowledge
  completion, conflicts stated, missing info stated, no restricted-document
  leakage). E-013 honours these in the conservative-refusal wording and by
  never injecting facts of its own.
- **build plan §16.3 / §16.4** — Claim Map + Claim Verifier (Internal MVP slice
  only: bind key claims to the Evidence actually used, citations resolvable,
  clearly `unsupported` claims do not enter the final answer).
- **build plan §16.5 / §16.6** — citation format + immutable references.

## in_scope
- **Tenant binding (fail-closed)** — `build_answer_envelope` / `conservative_refusal`
  assert `ctx.tenant_id == result.tenant_id` and that every cited Evidence
  snapshot's `tenant_id`/`corpus_id` matches the result (build plan §12.8 / M2
  single-tenant invariant). A mismatch raises `TenantBindingError`; cross-tenant
  Evidence can never be wrapped by another tenant's Context.
- Typed `AnswerEnvelope` (build plan §7.9, single-corpus/single-iteration
  slice) with `request_id`/`session_id` from the `SecurityContext`, the
  `evidence` snapshots, `claims`, `completeness`, `confidence`,
  `missing_aspects`, `limitations`, `corpora_used`, `iterations` (=1),
  `tool_calls` (=1), `stop_reason`, `abstained`.
- Typed, **frozen** `Claim` (claim_id, text, importance, `evidence_ids` as an
  immutable tuple, support_status) and **frozen** `Citation` (1-based UI index +
  immutable source refs).
- **Citation rendering** — `render_citations(evidence)` maps each E-011
  snapshot to a `Citation` carrying `corpus_id`, `document_id`, `document_version`,
  `section_path`, `page_number`, `source_uri`, `text_hash`, `retrieved_at`,
  `policy_version`; `format_citation_panel` emits the UI `[n] …` list. Every
  citation is resolvable to a snapshot; no dangling citation is allowed (the
  envelope validator checks `Citation.evidence_id` too).
- **Single key-claim support verification** — `verify_claims(claims, evidence)`
  deterministically treats a claim as `unsupported` (and removes it from the
  final answer) when ANY of: its `support_status` is already `unsupported`; it
  carries no `evidence_ids`; or any `evidence_ids` entry fails to resolve to the
  used Evidence. A removed *critical* claim is recorded so `completeness` is
  downgraded to `partial`. No LLM Judge, no regeneration loop (those are
  deferred).
- **Unsupported claims never reach the final answer** —
  `build_answer_envelope` always renders `answer_markdown` from the *kept*
  (supported) claims only (`_render_answer_from_claims`), so an unsupported
  claim's fact cannot appear in the answer text. Missing or empty `claims` fail
  closed to a generic `partial` response; caller-supplied prose is never returned
  without a verified Claim map.
- **Conservative refusal** — when `FastPathResult` is `insufficient`, build an
  `abstained` envelope: empty `claims`, empty `evidence`, `completeness ==
  insufficient`, `confidence == low`, `stop_reason == no_evidence`, and a fixed
  generic refusal string that mentions no document name or content. No
  fabricated facts are produced. `conservative_refusal` raises
  `AnswerEnvelopeError` if called with a `sufficient` result.
- A `model_validator` on `AnswerEnvelope` locks the state combinations (no
  dangling claim/citation id; `abstained` ⇒ empty claims/evidence +
  `completeness == insufficient` + `stop_reason == no_evidence`;
  `insufficient`/`no_evidence` ⇒ `abstained`), mirroring the E-012 validated-model
  approach.

## deferred_to
- **E-014** — the shared chat application service + LLM synthesis that produces
  `answer_markdown` and extracts `claims` (E-013 only *wraps* and *verifies*
  them). FastAPI `/v1/chat` and the Gradio adapter also belong to E-014.
- **E-019 / E-020** — Required-Fact LLM Judge, multi-model semantic entailment,
  Judge calibration, automatic claim decomposition, and regeneration/rewrite
  loops. E-013's verification is the deterministic, single-pass MVP slice only.
- Release-scale citation-entailment / coverage metrics (build plan §353) and
  their calibration.
- Planner / multi-corpus / multi-hop (later milestones).

## allowed_paths (M2 only)
- `src/agentic_rag_enterprise/answer/__init__.py` (new) — exports.
- `src/agentic_rag_enterprise/answer/envelope.py` (new) — `AnswerEnvelope`,
  `Claim`, `Citation`.
- `src/agentic_rag_enterprise/answer/citations.py` (new) — `render_citations`,
  `format_citation_panel`.
- `src/agentic_rag_enterprise/answer/verification.py` (new) — `verify_claims`,
  `ClaimVerificationResult`.
- `src/agentic_rag_enterprise/answer/builder.py` (new) —
  `build_answer_envelope`, `conservative_refusal`.
- `tests/unit/test_answer_envelope.py` (new) — focused unit tests.
- `docs/issue-e013-contract.md` (this file).
- `AGENTS.md` — update Current Milestone & Issue.
- **Reuse, no change:** `retrieval/fast_path.py` (`FastPathResult`,
  `FastPathSufficiency`, `FastPathStopReason`), `domain/evidence.py`,
  `domain/security.py`, `retrieval/__init__.py` (exports already present).
- **Must NOT touch:** `agents/synthesis.py` and `schemas.GroundedAnswer` (M0
  baseline mocks kept green for characterization tests); `agents/planner.py`;
  any E-011 / E-012 module.

## forbidden
- No LLM call / natural-language answer synthesis inside E-013 (deferred to
  E-014). E-013 accepts `answer_markdown` and `claims` as inputs.
- No reuse of `schemas.GroundedAnswer` / `schemas.Evidence` (M0 mocks) as the
  E-013 output type; the real `AnswerEnvelope` / E-011 `Evidence` are used.
- No cross-tenant / cross-corpus wrapping: a `tenant-a` Context must never wrap
  `tenant-b` Evidence (fail-closed `TenantBindingError`).
- No multi-model Judge, claim decomposition, calibration, or regeneration loop.
- No fabricated facts on the abstain path; the refusal string must not reveal
  document names or content (build plan §16.2).
- No unsupported claim in the final answer: an `unsupported`/evidence-less claim
  is removed and its fact must not appear in `answer_markdown`; `Claim` and
  `Citation` are frozen and may not be mutated after construction.
- No dangling citation: every `Claim.evidence_ids` entry and every
  `Citation.evidence_id` must resolve to an Evidence Snapshot in the envelope
  (validator enforces this).
- No Planner / Typed DAG / multi-corpus / multi-hop.
- No modification of E-011 or E-012 behaviour.
- No reserved/placeholder modules, DB tables, or runtime branches not exercised
  by the E-013 tests.
- No upstream modifications.

## acceptance_tests
- `tests/unit/test_answer_envelope.py` —
  - `test_sufficient_path_builds_envelope_with_resolvable_citations`: a
    `sufficient` `FastPathResult` + caller answer + claims → `AnswerEnvelope`
    with `abstained is False`, `completeness == complete`, `iterations == 1`,
    `tool_calls == 1`, `corpora_used == [corpus_id]`, and every rendered
    citation resolvable to a snapshot (carries `document_version`/`text_hash`/
    `policy_version`).
  - `test_insufficient_path_produces_abstained_refusal`: an `insufficient`
    `FastPathResult` → `abstained is True`, `claims == []`, `evidence == []`,
    `completeness == insufficient`, `confidence == low`, `stop_reason ==
    no_evidence`, refusal string contains no document name/content and no
    fabricated fact.
  - `test_tenant_mismatch_between_ctx_and_result_rejected` /
    `test_cross_tenant_evidence_rejected` / `test_cross_corpus_evidence_rejected`:
    any tenant/corpus mismatch raises `TenantBindingError` (fail-closed).
  - `test_explicitly_unsupported_claim_removed` /
    `test_missing_claims_fail_closed_without_returning_caller_text` /
    `test_empty_claims_fail_closed_without_returning_caller_text` /
    `test_critical_claim_with_empty_evidence_downgraded_and_removed` /
    `test_dangling_evidence_id_claim_removed`: unsupported / evidence-less /
    unresolved claims are removed and their facts never appear in
    `answer_markdown`; missing/empty Claim maps fail closed; a removed critical
    claim downgrades `completeness` to `partial`.
  - `test_claim_and_citation_are_frozen` / `test_dangling_claim_rejected_by_validator`
    / `test_dangling_citation_rejected_by_validator`: `Claim`/`Citation` are
    frozen (no post-construction mutation) and both claim and citation ids must
    resolve to an Evidence Snapshot.
  - `test_conservative_refusal_rejects_sufficient_result` /
    `test_abstained_envelope_state_locked`: `conservative_refusal` rejects a
    `sufficient` result; an `abstained` envelope must be locked to empty
    claims/evidence + `completeness == insufficient` + `stop_reason == no_evidence`.
- Regression that MUST stay green: E-011 (`tests/unit/test_deduplication.py`,
  `tests/unit/test_evidence_store.py`, `tests/integration/test_e011_evidence_pipeline.py`),
  E-012 (`tests/unit/test_fast_path.py`), `tests/unit/test_retrieval_boundary.py`,
  `tests/baseline/`.
- Quality gates: `ruff check`, `ruff format --check` (whole tree, incl. the 9
  pre-existing files now reformatted), `mypy src/agentic_rag_enterprise`,
  `git diff --check` all clean.

## acceptance_commands
```bash
# E-013 focused unit suite (run tonight)
.venv/bin/python -m pytest tests/unit/test_answer_envelope.py -q

# Must remain green (no regression of E-011 / E-012 / boundary / baseline)
.venv/bin/python -m pytest tests/unit -q

# Quality gates (run tonight)
.venv/bin/ruff check src/agentic_rag_enterprise tests
.venv/bin/ruff format --check src/agentic_rag_enterprise tests
.venv/bin/mypy src/agentic_rag_enterprise
git diff --check
```
