# Issue E-016 — Permission-aware Soft Router + Cross-Corpus Retrieval, Merge & Dedup

**Milestone:** M4 (Multi-Corpus Retrieval) — build plan §9 / Milestone 4
**Depends on:** E-015 (Corpus/Capability Registry + fixtures + discoverability)
**Status:** implemented; committed after acceptance.

This document is the versioned contract for E-016. It is a strict continuation of
E-015: the registry is the single source of discoverable corpora, and E-016 only
adds the *data-plane* slice — routing, cross-corpus retrieval, evidence merge/dedup
and a multi-corpus application entry. No Planner DAG, no multi-hop dependency, no
Required-Fact Judge, no iteration, no authority/freshness conflict resolution, no
SQL/API/graph capability. Those are later milestones (M5 / E-017→E-018 and beyond).

---

## Goals

1. **Permission-aware soft router (build plan §9.3)** — deterministically select
   the corpora the caller may see, *only* from `CorpusRegistry.resolve_candidates(...)`
   output. Scoring is **query-sensitive** and normalized to `[0, 1]`: a query-term /
   corpus-term relevance signal combined with the registry-declared `authority_level`.
   The router emits `route_confidence` (`high`/`medium`/`low`) and `fallback_search`,
   and applies the §9.3 policy: `high` → Top-1, `medium` → Top-2, `low` → Top-3 +
   fallback (never hard-route an unmatched query to raw authority). The model never
   sees the full corpus map.
2. **Cross-corpus retrieval** — run the *existing* `SecureRetriever.retrieve_evidence`
   per selected corpus, passing the same `SecurityContext`. Each corpus keeps its
   tenant / ACL / active-version / parent second-auth constraints. A single-corpus
   backend fault is surfaced as an explicit fault and is **never** relabelled as
   "no Evidence".
3. **Evidence merge & dedup** — combine authorized Evidence from multiple corpora,
   dedup by stable Evidence id / text hash / document version, preserve source
   attribution (the contributing corpora are recorded), and produce a *deterministic*
   output order so tests and eval reports do not drift.
4. **Application entry** — a new, explicit multi-corpus mode on the chat service
   (`answer_multi_corpus`). The single-corpus `answer()` and `answer_with_iteration`,
   the E-012 Fast Path and the E-013/E-019/E-020 envelope all stay unchanged.
   `AnswerEnvelope.corpora_used` reflects the corpora that *actually contributed*
   Evidence — not the full candidate set.

## Non-goals / forbidden (per build plan §9 and the M4 scope)

- No Planner DAG, no dependency-based multi-hop retrieval.
- No new Required-Fact Judge or iteration policy in the multi-corpus path.
- No authority-level / freshness conflict arbitration between corpora.
- The SQL / API / graph capabilities remain reserved-but-not-enabled (E-015).
- No M5 protocol changes; no changes to E-011→E-015, E-019, E-020 behaviour.

---

## Allowed paths (M4 only)

- `docs/issue-e016-contract.md` — this contract.
- `src/agentic_rag_enterprise/corpus/router.py` — NEW: `CorpusCandidate`,
  `CorpusRoute`, `CorpusRouter` (deterministic scoring; input constrained to
  registry candidates; no full-map exposure).
- `src/agentic_rag_enterprise/retrieval/multi_corpus.py` — NEW:
  `MultiCorpusResult`, `CorpusRetrievalFault`, `MultiCorpusRetrieval`
  (per-corpus `SecureRetriever.retrieve_evidence`, merge + dedup).
- `src/agentic_rag_enterprise/services/chat_service.py` — extend with
  `answer_multi_corpus(query, ctx, *, corpus_ids=None)` that uses the router +
  multi-corpus retrieval and the existing single-pass synthesis. `answer()` and
  `answer_with_iteration` are NOT modified.
- `src/agentic_rag_enterprise/corpus/__init__.py` — export router symbols.
- `src/agentic_rag_enterprise/answer/builder.py` — NEW multi-corpus binding
  builder `build_multi_corpus_envelope(...)` + shared `_build_envelope_from_evidence`
  / `_build_refusal` helpers and an optional `limitations` / `partial_retrieval`
  degrade path (E-016 partial-fault semantics). `build_answer_envelope` /
  `conservative_refusal` behaviour is preserved (single-corpus paths unchanged).
- `tests/unit/corpus/test_router.py`, `tests/unit/retrieval/test_multi_corpus.py`,
  `tests/security/test_multi_corpus_isolation.py`,
  `tests/integration/test_e016_multi_corpus_pipeline.py`.
- `AGENTS.md` — record E-016 CLOSED.

### Reuse, no change

- `retrieval/retriever.py` `SecureRetriever.retrieve_evidence` (per-corpus call).
- `corpus/registry.py` `CorpusRegistry` / `InMemoryCorpusRegistry`.
- `corpus/capability_registry.py` `CapabilityCatalog`.
- `answer/envelope.py` `AnswerEnvelope` (`corpora_used` / `limitations` /
  `tool_calls` already exist; no model change).
- `retrieval/fast_path.py` (single-corpus only; not reused in the multi-corpus path).
- `domain/evidence.py`, `domain/security.py`, `domain/corpus.py`, `config.py`,
  `providers.py`.

---

## Data model

### Router (`corpus/router.py`)

```python
@dataclass(frozen=True)
class CorpusCandidate:
    corpus_id: str
    name: str
    authority_level: int
    relevance: float        # query-term overlap signal, [0, 1]
    score: float            # 0.7*relevance + 0.3*(authority/100), clamped [0, 1]
    rationale: str          # short, non-leaky reason (never includes denied corpora)

@dataclass(frozen=True)
class CorpusRoute:
    query: str
    candidates: tuple[CorpusCandidate, ...]   # policy-selected, deterministic
    route_confidence: Literal["high", "medium", "low"]
    fallback_search: bool
    truncated_from: int                       # how many registry candidates existed

CorpusRouter.route(
    query, ctx, registry, *, limit: int | None = None
) -> CorpusRoute
```

- Input is **only** `registry.resolve_candidates(query, ctx, limit=...)` (all
  discoverable, capability-eligible corpora). The router never receives, and never
  emits, a non-discoverable corpus. No `list`/`dict` of the whole corpus map is
  handed to any model or returned to the caller.
- Scoring is **deterministic and query-sensitive** (build plan §9.3): `relevance`
  is the normalized overlap between the query's tokens and the corpus'
  name/description/domain/id/capability term bag (stopword-filtered), in `[0, 1]`;
  `score = _RELEVANCE_WEIGHT * relevance + _AUTHORITY_WEIGHT * (authority/100)`
  (fixed weights `0.7`/`0.3`), clamped to `[0, 1]`. No LLM. Ranked stably by
  `(score desc, authority desc, corpus_id asc)`.
- `route_confidence` / `fallback_search` / candidate count follow §9.3:
  `high` (dominant, strongly-relevant top-1) → Top-1, no fallback; `medium` (some
  relevance, no dominant winner) → Top-2, no fallback; `low` (query matched no
  corpus term) → Top-3 + `fallback_search=True` (broaden + probe; never hard-route
  to authority). An explicit `limit` only truncates the policy count, never widens it.
- `rationale` is derived purely from the *selected* candidates (e.g.
  `"relevance=0.50 authority=80"`); it must not reference any denied/undiscoverable
  corpus.

### Multi-corpus retrieval (`retrieval/multi_corpus.py`)

```python
@dataclass(frozen=True)
class CorpusRetrievalFault:
    corpus_id: str
    reason: str            # generic, non-leaky
    error_type: str        # e.g. "FastPathBackendError", "ValueError"

@dataclass(frozen=True)
class MultiCorpusResult:
    evidence: tuple[Evidence, ...]          # merged, deduped, deterministic order
    corpora_used: tuple[str, ...]           # corpora that CONTRIBUTED evidence
    routed: tuple[str, ...]                 # corpus ids the router selected
    faults: tuple[CorpusRetrievalFault, ...]  # backend faults, NOT "no evidence"
    insufficient_corpora: tuple[str, ...]  # routed but returned zero evidence

MultiCorpusRetrieval.retrieve(
    ctx, query, corpora: list[CorpusConfig], *, top_k=None
) -> MultiCorpusResult
```

- For each selected corpus, call `SecureRetriever.retrieve_evidence` with the same
  `ctx`. Propagate the *same* `SecurityContext` so per-corpus tenant/ACL/active-version/
  parent-second-auth all apply.
  - **Fault handling** (fail-loud, never fail-open, explicit type boundary):
    - `MultiCorpusRetrieval` catches **only** `RetrievalBackendError` (a new,
      explicit backend/infrastructure exception type in `retrieval/models.py`).
      Adapter code raises it for genuine infra faults; a curated set of transport
      errors (`ConnectionError`, `TimeoutError`, qdrant `UnexpectedResponse` /
      `ResponseHandlingException`) is converted to it by `retrieval/backend_fault.py`.
      A backend fault is captured as a `CorpusRetrievalFault` and never relabelled as
      "no Evidence"; the other corpora's evidence is still returned.
    - **Security / authorization / binding / configuration errors propagate
      immediately** (`CorpusNotDiscoverableError`, `ParentAuthorizationError`,
      `EmptyAuthorizationScopeError`, `TenantBindingError`). They are never downgraded
      to a partial fault, even when a sibling corpus succeeds — a denial is never
      masked.
    - **Programming errors (`ValueError` / `TypeError` / `KeyError` / `AssertionError`
      / adapter bugs) are NOT captured** — they propagate untouched, so a sibling's
      evidence can never mask a real bug (this is the explicit type boundary that
      replaces the old broad `except Exception`).
    - `retrieve` raises **only when every selected corpus faults** (`len(faults) ==
      len(corpora)`) — a single fault alongside a legitimately-empty sibling is NOT a
      total outage.
    - **Cross-corpus Evidence binding** — every returned snapshot is re-checked to
      match the requested tenant and the corpus it was requested from; a mismatch is a
      `TenantBindingError` (security violation), never a fault, never merged.
    - `MultiCorpusResult.retrieval_calls` records the true number of per-corpus
      retrieval calls executed (for a truthful envelope `tool_calls`).
  - **Merge & dedup** (`merge_evidence` → `MergeResult`) — two layers, deterministic:
    - Iterate corpora in ascending `corpus_id` order, evidence in input order.
    - **Layer 1 — stable `evidence_id` dedup, first occurrence wins.** A repeated
      `evidence_id` is dropped *before* content folding, so one `evidence_id` can never
      map to two non-interchangeable snapshots (different text/version/corpus). First
      occurrence (corpus_id asc, then input order) is authoritative.
    - **Layer 2 — cross-id same-content folding.** Two Evidence sharing
      `(text_hash, document_id, document_version)` but a *different* `evidence_id`
      collapse to the higher `authority_level` (tie → existing survivor). The loser's
      `corpus_id` is still marked as contributed (source attribution preserved), but
      only one primary Evidence is emitted.
    - **Different `document_version` is NOT folded** — same text, different version
      stays as distinct Evidence.
    - `MergeResult.contributing_corpora` = the corpora that emitted at least one
      *surviving* primary or folded Evidence. A corpus whose raw snapshots were all
      dropped by Layer-1 stable-id dedup does **not** count — `corpora_used` reflects
      real contribution, not "returned some raw rows" (P2-1). `insufficient_corpora` =
      routed corpora that returned zero evidence and did not fault.

### Chat service (`services/chat_service.py`)

```python
ChatService.answer_multi_corpus(
    query: str, ctx: SecurityContext, *, corpus_ids: list[str] | None = None
) -> AnswerEnvelope
```

- When `corpus_ids` is `None`, route via `CorpusRouter.route(query, ctx, registry)`
  (the §9.3 policy decides the count — Top-1/2/3; `router_limit` defaults to `None`
  so the policy is never truncated by a hard `limit=2`). Use the selected
  `candidates`. Otherwise restrict to the explicitly requested (and still
  discoverable) `corpus_ids`. A requested-but-undiscoverable corpus fails closed
  (never silently dropped into retrieval).
- **§9.3 fallback expansion (strictly scoped)** — the `CorpusRoute` exposes a
  `fallback_candidates` entry **only for a `high`-confidence Top-1 route with no
  explicit `limit`**; `medium` / `low` routes and any `limit`-truncated route leave
  it empty, so a hard `router_limit` cap can never be bypassed by smuggling a second
  corpus in via fallback. `answer_multi_corpus` only expands when ALL of
  (`corpus_ids is None` AND `router_limit is None` AND `route_confidence == "high"` AND
  primary Evidence is empty); on expansion it queries **only the new fallback corpus**
  and merges the result with the primary (the primary is never re-queried), so
  `retrieval_calls` / `tool_calls` equals the true call count (primary × 1 +
  fallback × 1). §9.3 forbids hard-routing a miss straight to "no answer".
- **Registry is the config source of truth** — the `CorpusConfig` handed to
  retrieval comes ONLY from `registry.get(corpus_id, ctx)` (for both routed and
  explicit ids, including fallback candidates); the legacy single-corpus
  `_resolve_corpus` resolver is never used on the multi-corpus path, so a
  stale/duplicate resolver config can never reach retrieval.
- Run multi-corpus retrieval, then **single-pass** synthesis
  (`build_multi_corpus_envelope` with the merged evidence; no judge, no iteration).
  `corpora_used` is set from `MultiCorpusResult.corpora_used`; `tool_calls` from
  `MultiCorpusResult.retrieval_calls`.
- **Security / binding errors propagate in original type** — `answer_multi_corpus`
  re-raises `CorpusNotDiscoverableError` / `ParentAuthorizationError` /
  `EmptyAuthorizationScopeError` / `TenantBindingError` *before* any wrapping; only a
  genuine `RetrievalBackendError` is rewrapped as `FastPathBackendError` (5xx). A
  security denial is never relabelled as a backend fault.
- **Partial-fault degrade + limitations** — if there is evidence AND at least one
  captured fault, the envelope is built from the available evidence but carries an
  explicit partial-retrieval `limitations` entry and is degraded from
  `complete`/`high` to `partial`/`medium` (never reported as unconditionally
  complete). If the merged evidence is empty and there are no faults →
  `conservative_refusal` (the existing abstain lock). If evidence is empty and
  *every* corpus faulted → `retrieve` already raised. **If evidence is empty but a
  partial fault exists** (one corpus down, sibling legitimately empty) → the service
  raises (`FastPathBackendError`), it is **never** emitted as an ordinary
  `no_evidence` abstain (P1-4).

---

## Acceptance criteria (core scenarios)

1. **Isolation** — when the caller is authorized for only 2 of 3 corpora, the third
   is invisible end-to-end: absent from router input, from retrieval requests, from
   `evidence`, and from `corpora_used`. Its name/description never leaks.
2. **Single-corpus question** — passing one `corpus_id` (or a router that selects
   one) results in exactly one `SecureRetriever.retrieve_evidence` call.
3. **Comparison question** — two authorized corpora are selected and both are
   retrieved; their authorized Evidence is merged into one result.
4. **Stable dedup** — two corpora returning *identical text* (same hash + version)
   yield exactly one primary Evidence, with both corpora recorded in `corpora_used`.
5. **Version not folded** — identical text under *different* `document_version`
   yields two distinct Evidence (not collapsed).
6. **Fault semantics** — when one corpus's retrieval raises a *backend* fault, it is
   captured in `faults`, the other corpus's evidence is still returned, and the
   envelope degrades to `partial`/`medium` with an explicit `limitations` entry; a
   *total* fault (all corpora) raises (never becomes an abstain).
7. **Security never masked** — a security/authorization/binding error
   (`CorpusNotDiscoverableError`, `ParentAuthorizationError`,
   `EmptyAuthorizationScopeError`, `TenantBindingError`) propagates fail-closed even
   when a sibling corpus succeeds; cross-corpus/cross-tenant Evidence is rejected.
8. **Query-sensitive routing (§9.3)** — a query that matches a corpus' terms routes
   there with the right `route_confidence`/count; an unmatched query yields `low`
   confidence + `fallback_search`, never a hard-route on raw authority alone.
9. **`corpora_used` / `tool_calls` truthful** — only corpora that actually
   contributed Evidence appear; `tool_calls` equals the executed retrieval calls.
10. **No regression** — E-011→E-020, E-015 and `tests/baseline/` all stay green;
    `ruff`, `ruff format`, `mypy src/agentic_rag_enterprise` clean.

## Quality gates

- `ruff check src/agentic_rag_enterprise/corpus/router.py src/agentic_rag_enterprise/retrieval/multi_corpus.py`
- `ruff format --check .`
- `uv run mypy src/agentic_rag_enterprise`
- `pytest tests/unit/corpus tests/unit/retrieval tests/security/test_multi_corpus_isolation.py tests/integration/test_e016_multi_corpus_pipeline.py tests/baseline`
