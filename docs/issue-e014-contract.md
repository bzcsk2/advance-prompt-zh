# E-014 Issue Contract (M2) — shared chat application service, synchronous /v1/chat, minimal Gradio adapter

Final capability of Milestone 2 (single-corpus Internal MVP, build plan §2.2 /
§5 / §6). Completes the Internal MVP run-chain:

```text
ingest / update / delete  →  authorized hybrid retrieve  →  deduplicate + snapshot
→  one-pass sufficiency  →  grounded answer + citations / abstain
→  synchronous API or Gradio
```

E-014 wires the already-built layers into a single, reusable **application
service** that backs BOTH a synchronous `POST /v1/chat` FastAPI endpoint and a
minimal Gradio chat UI:

* **E-012** `run_fast_path` — one-pass sufficient / insufficient decision, exactly
  one `retrieve_evidence` call, typed `FastPathBackendError` on retrieval fault.
* **E-011** `domain.evidence.Evidence` — the immutable snapshots the answer is
  grounded on and cited from.
* **E-013** `build_answer_envelope` / `conservative_refusal` — wraps the result
  into a validated, frozen `AnswerEnvelope`. **E-013 fails closed**: the final
  answer is *always* derived from the verified (kept) `Claim`s; the LLM draft
  prose is advisory only and is never returned.

E-014 is the ONLY place that calls an LLM. The LLM is used for two things, and
both are advisory / fail-closed:

1. Extract atomic `Claim`s, each bound to real `evidence_id`s from the retrieved
   Evidence (the draft `answer_markdown` is also produced, but per E-013 it is
   discarded — only verified claims reach the answer).
2. It is **never** allowed to produce or modify security context fields
   (`tenant_id`, `user_id`, `roles`, `policy_version`, …) — those are strictly
   runtime-injected (build plan §5.4).

## depends_on
- **E-012** — `run_fast_path`, `FastPathResult`, `FastPathBackendError`. The
  service calls `run_fast_path` exactly once and propagates `FastPathBackendError`.
- **E-011** — `domain.evidence.Evidence` (the snapshot type passed into the LLM
  prompt as grounding context, and the only citation source).
- **E-013** — `build_answer_envelope`, `conservative_refusal`, `Claim`,
  `AnswerEnvelope`. The service calls `build_answer_envelope` on `sufficient` and
  `conservative_refusal` on `insufficient`; it must honour E-013's fail-closed
  rule (answer derived from kept claims only).
- **`providers.py`** — `ModelProvider` / `StructuredProvider` protocol,
  `FakeModel`, `create_provider`, `ModelProfile(purpose="synthesis")`. E-014 uses
  these to obtain a model; only `provider="fake"` is implemented today (real
  Ollama/OpenAI dispatch is deferred).
- **`config.py`** — `Settings` (qdrant url, metadata db path, `max_retrieval_top_k`).
- **build plan §5.4** — security boundary: tenant/identity fields are runtime-injected.
- **build plan §6** — target code structure (`services/`, `api/`, `ui/`).

## in_scope
- **Application service (`services/chat_service.py`)** — `ChatService` with a
  synchronous method `answer(query, ctx, corpus_id) -> AnswerEnvelope` (or an
  equivalent explicit signature). It:
  1. resolves the `CorpusConfig` for `corpus_id` via an injected
     `resolve_corpus: Callable[[str], domain.corpus.CorpusConfig]`;
  2. calls `run_fast_path(retriever, ctx, query, corpus, top_k=…,
     dense_encoder=…, sparse_encoder=…)` exactly once;
  3. on `sufficient`: builds the grounding prompt from `result.evidence`
     (carrying each `evidence_id`), calls the model's
     `with_structured_output(ClaimExtraction)` to get a draft `answer_markdown`
     + `claims`, then `build_answer_envelope(result, ctx, answer_markdown=draft,
     claims=claims)`;
  4. on `insufficient`: `conservative_refusal(result, ctx)`;
  5. lets `FastPathBackendError` and any model/provider error propagate (typed),
     and **never** relabels a fault as a grounded answer or a refusal.
  `ChatService.__init__` receives already-constructed dependencies
  (`retriever: SecureRetriever`, `dense_encoder`, `sparse_encoder`,
  `model: ModelProvider`, `resolve_corpus`, `top_k: int`), so it is fully
  unit-testable with fakes. A thin `services/composition.py` factory
  (`build_chat_service(...)`) wires the real storage + model for the API/Gradio
  entry points (acceptable scope; no behaviour change to E-011/E-012/E-013).
- **LLM structured extraction schema (`services/claims_schema.py` or local)**
  — `ClaimExtraction(BaseModel)` with `draft_answer: str` and
  `claims: list[Claim]` (reusing E-013's frozen `Claim`). The service passes the
  retrieved Evidence (with `evidence_id`s) into the prompt so the model can bind
  claims to real ids. No security fields are ever sent to, or read back from, the
  model.
- **Synchronous `POST /v1/chat` (`api/routes/chat.py`, wired in `api/main.py`)**
  — the endpoint is an *adapter only* (no business rules): it builds a
  `SecurityContext` from the trusted request metadata (runtime-injected), calls
  `ChatService.answer`, and returns `AnswerEnvelope.model_dump()`. It must NOT
  expose `denied_reasons` / internal telemetry. No SSE / streaming (deferred).
  Returns 422 on validation failure, 500 on backend/model fault.
- **Minimal Gradio adapter (`ui/gradio_app.py`)** — `build_gradio_app(service)`
  returns a small chat UI that calls `ChatService.answer`. `gradio` is **not** a
  current dependency and is **not** required for the quality gates; the module
  therefore imports `gradio` *lazily* inside `build_gradio_app` so it is
  import-safe without gradio installed, and raises a clear
  `RuntimeError` (or the test skips) when gradio is absent. Adding gradio as an
  optional extra in `pyproject.toml` is in scope; installing it is NOT required
  for the unit gates.
- **Corpus resolution uses `domain.corpus.CorpusConfig`** — the service resolves
  to the *domain* `CorpusConfig` (the type `run_fast_path`/`SecureRetriever`
  require), NOT the M0 baseline `schemas.CorpusConfig` that `retrieval/corpus_registry.py`
  returns. A minimal single-corpus resolver (e.g. from `configs/corpora.yaml` →
  `domain.corpus.CorpusConfig`, or an in-code registry) is in scope; full
  multi-corpus `CorpusRegistry` routing is deferred to later milestones.
- **Typed service errors** — `ChatServiceError(Exception)` (base) and a
  `ModelInvocationError(ChatServiceError)` used when the model provider raises, so
  a model outage surfaces as a 5xx and is never silently turned into a refusal or
  a fabricated answer.
- **AGENTS.md** — mark E-014 CLOSED and record the Internal MVP completion.

## deferred_to
- **E-015 … E-027 (Milestone 3–8)** — multi-Corpus Registry + soft routing,
  Typed Planner DAG, complexity router, multi-hop, Required-Fact LLM Judge,
  gap-driven iteration, conflict resolution, reranker, Langfuse traces,
  evaluation harness (build plan §2.2 / §2.3 non-goals).
- **Real LLM providers** — Ollama / OpenAI / etc. dispatch beyond `provider="fake"`
  (only the protocol + `FakeModel` exist today; `create_provider` raises
  `UnsupportedProviderError` for others).
- **SSE / streaming** and async workflows (explicit non-goals for Internal MVP).
- **Production auth middleware** — the MVP injects the `SecurityContext` from
  request metadata (a stand-in for gateway/IAM injection). Real token/SSO
  verification is an Enterprise-MVP concern.
- **Gradio production hardening** (themes, auth, deployment) — the MVP adapter is
  minimal and import-safe.

## allowed_paths (M2 only)
- `src/agentic_rag_enterprise/services/__init__.py` (new) — exports.
- `src/agentic_rag_enterprise/services/chat_service.py` (new) — `ChatService`,
  `ChatServiceError`, `ModelInvocationError`, `answer`.
- `src/agentic_rag_enterprise/services/composition.py` (new, optional) —
  `build_chat_service` factory wiring real storage + model.
- `src/agentic_rag_enterprise/services/claims_schema.py` (new) —
  `ClaimExtraction` structured-output schema.
- `src/agentic_rag_enterprise/api/main.py` (edit) — register the `/v1/chat` router.
- `src/agentic_rag_enterprise/api/routes/chat.py` (new) — `POST /v1/chat` adapter.
- `src/agentic_rag_enterprise/api/schemas.py` (new) — `ChatRequest`, `ChatResponse`.
- `src/agentic_rag_enterprise/api/dependencies.py` (new) — `SecurityContext`
  construction from request metadata.
- `src/agentic_rag_enterprise/ui/__init__.py` (new) — exports.
- `src/agentic_rag_enterprise/ui/gradio_app.py` (new) — minimal lazy-import
  Gradio adapter.
- `configs/corpora.yaml` (new, optional) — the single MVP corpus definition
  (domain `CorpusConfig` shape).
- `tests/unit/test_chat_service.py` (new) — focused service tests.
- `tests/unit/test_chat_api.py` (new) — `/v1/chat` adapter tests.
- `tests/unit/test_gradio_app.py` (new) — import-safe + lazy-import tests.
- `docs/issue-e014-contract.md` (this file).
- `AGENTS.md` — update Current Milestone & Issue.
- **Reuse, no change:** `retrieval/fast_path.py`, `answer/*`, `domain/evidence.py`,
  `domain/security.py`, `domain/corpus.py`, `providers.py`, `config.py`.
- **Leave untouched (baseline):** the legacy `POST /chat` (graph M0 mock) remains
  for characterization; it is NOT the enterprise path and is out of MVP scope.
  `retrieval/corpus_registry.py` (M0 `schemas.CorpusConfig`) is NOT used by the
  Fast Path path.

## forbidden
- No Planner / Typed DAG / multi-corpus / multi-hop / reranker (later milestones).
- No LLM Required-Fact Judge, claim decomposition, calibration, or regeneration
  loop (deferred to E-019/E-020). E-014's claim verification is only what E-013
  already does deterministically.
- No modification of E-011 / E-012 / E-013 behaviour or types. The service only
  *calls* them.
- **No security-context fields from the LLM.** `tenant_id`, `user_id`, `roles`,
  `groups`, `allowed_security_levels`, `allowed_corpus_ids`, `policy_version`,
  `is_admin`, `permissions` are constructed at the API boundary from trusted
  request metadata and passed into the service; they are never read from, or
  influenced by, model output (build plan §5.4).
- **No masking of faults as answers or refusals.** `FastPathBackendError` and any
  model/provider exception propagate as typed errors (→ 5xx). A retrieval outage
  or model outage must never be relabelled as "no answer" / a grounded reply.
- **No returning the raw LLM draft.** Per E-013 fail-closed, `answer_markdown`
  comes ONLY from verified claims; the LLM `draft_answer` is advisory/observability
  only and must not reach the response.
- No SSE / streaming / async workflows.
- No cross-tenant / cross-corpus leakage: the `SecurityContext` tenant and the
  resolved corpus tenant must match (fail-closed via E-007 `CorpusNotDiscoverableError`
  / E-013 `TenantBindingError`).
- No `schemas.CorpusConfig` (M0 mock) fed into `run_fast_path` / `SecureRetriever`
  — only `domain.corpus.CorpusConfig` is accepted.
- No top-level `import gradio` in `ui/gradio_app.py` (must stay import-safe without
  gradio installed).
- No upstream modifications; no reserved/placeholder modules, DB tables, or
  runtime branches not exercised by the E-014 tests.

## acceptance_tests
- `tests/unit/test_chat_service.py` —
  - `test_sufficient_path_returns_envelope_from_verified_claims`: fake model
    returns `Claim`s bound to real `evidence_id`s → `AnswerEnvelope` has `abstained
    is False`, `completeness in {complete, partial}`, `iterations == 1`,
    `tool_calls == 1`, `corpora_used == [corpus_id]`, and the returned
    `answer_markdown` is rendered from the kept claims (NOT the LLM `draft_answer`
    verbatim).
  - `test_insufficient_path_returns_abstained_refusal`: `run_fast_path` returns
    `insufficient` → `conservative_refusal` → `abstained is True`, `claims == []`,
    `evidence == []`, `completeness == insufficient`, `stop_reason == no_evidence`,
    refusal text reveals no document name/content.
  - `test_unsupported_claims_removed_and_draft_not_returned`: model returns an
    `unsupported` claim (or one citing an unknown id) → E-013 removes it, the
    unsupported fact does NOT appear in `answer_markdown`, and the LLM
    `draft_answer` is never returned.
  - `test_empty_claims_fail_closed`: model returns no claims → generic `partial`
    answer; `draft_answer` not returned.
  - `test_tenant_mismatch_propagates`: `ctx.tenant_id != corpus.tenant_id` → the
    `CorpusNotDiscoverableError` / `TenantBindingError` propagates (fail-closed,
    not a fabricated answer).
  - `test_backend_error_propagates_not_refusal`: faked `retrieve_evidence` raises
    → `FastPathBackendError` propagates (NOT converted to a refusal).
  - `test_model_error_propagates`: model provider raises → `ModelInvocationError`
    propagates (NOT a fabricated answer / refusal).
  - `test_security_context_never_from_model`: assert the prompt sent to the model
    carries only `query` + Evidence grounding (no security fields), and that the
    `SecurityContext` used by `build_answer_envelope` is the one injected into the
    service, not anything from model output.
- `tests/unit/test_chat_api.py` —
  - `POST /v1/chat` with a sufficient result returns 200 + `AnswerEnvelope` JSON
    with `abstained=false`, `iterations=1`, resolvable citations.
  - `POST /v1/chat` with an insufficient result returns 200 + abstained envelope.
  - the `SecurityContext` is built from request fields (tenant/user/policy), and
    `denied_reasons` / internal telemetry are NOT present in the response body.
  - malformed request → 422.
- `tests/unit/test_gradio_app.py` —
  - `ui/gradio_app.py` imports without gradio installed;
  - `build_gradio_app` raises a clear error (or the test skips) when gradio is
    absent, and constructs an interface that calls `ChatService.answer` when
    gradio is present.
- Regression that MUST stay green: E-011 (`tests/unit/test_deduplication.py`,
  `tests/unit/test_evidence_store.py`, `tests/integration/test_e011_evidence_pipeline.py`),
  E-012 (`tests/unit/test_fast_path.py`), E-013 (`tests/unit/test_answer_envelope.py`),
  `tests/unit/test_retrieval_boundary.py`, `tests/baseline/`.
- Quality gates: `ruff check`, `ruff format --check` (whole tree),
  `mypy src/agentic_rag_enterprise`, `git diff --check` all clean.

## acceptance_commands
```bash
# E-014 focused unit suite (run tonight)
.venv/bin/python -m pytest tests/unit/test_chat_service.py tests/unit/test_chat_api.py tests/unit/test_gradio_app.py -q

# Must remain green (no regression of E-011 / E-012 / E-013 / boundary / baseline)
.venv/bin/python -m pytest tests/unit tests/integration/test_e011_evidence_pipeline.py tests/unit/test_retrieval_boundary.py tests/baseline -q

# Quality gates (run tonight)
.venv/bin/ruff check src/agentic_rag_enterprise tests
.venv/bin/ruff format --check src/agentic_rag_enterprise tests
.venv/bin/mypy src/agentic_rag_enterprise
git diff --check
```
