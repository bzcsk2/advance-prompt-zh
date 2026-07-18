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

1. **Permission-aware soft router** — deterministically select the top-1 / top-2
   corpora the caller may see, *only* from `CorpusRegistry.resolve_candidates(...)`
   output. The model never sees the full corpus map.
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
- `tests/unit/corpus/test_router.py`, `tests/unit/retrieval/test_multi_corpus.py`,
  `tests/security/test_multi_corpus_isolation.py`,
  `tests/integration/test_e016_multi_corpus_pipeline.py`.
- `AGENTS.md` — record E-016 CLOSED.

### Reuse, no change

- `retrieval/retriever.py` `SecureRetriever.retrieve_evidence` (per-corpus call).
- `corpus/registry.py` `CorpusRegistry` / `InMemoryCorpusRegistry`.
- `corpus/capability_registry.py` `CapabilityCatalog`.
- `answer/envelope.py` `AnswerEnvelope` (`corpora_used` already exists).
- `answer/builder.py` `build_answer_envelope` / `conservative_refusal`.
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
    score: float            # deterministic route score
    rationale: str          # short, non-leaky reason (never includes denied corpora)

@dataclass(frozen=True)
class CorpusRoute:
    query: str
    candidates: tuple[CorpusCandidate, ...]   # ranked, deterministic, top-N only
    truncated_from: int                       # how many registry candidates existed

CorpusRouter.route(
    query, ctx, registry, *, limit: int = 2
) -> CorpusRoute
```

- Input is **only** `registry.resolve_candidates(query, ctx, limit=None)` (all
  discoverable, capability-eligible corpora). The router never receives, and never
  emits, a non-discoverable corpus. No `list`/`dict` of the whole corpus map is
  handed to any model or returned to the caller.
- Scoring is deterministic: `score = authority_level` (the only per-corpus signal
  available without an LLM; authority is a registry-declared, policy-reviewed value,
  not a model output), tie-broken by `corpus_id` ascending. Ranked stably; truncated
  to `limit`.
- `rationale` is derived purely from the *selected* candidates (e.g.
  `"authority=80"`); it must not reference any denied/undiscoverable corpus.

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
- **Fault handling** — a backend fault in one corpus is captured as a
  `CorpusRetrievalFault` and never relabelled as "no Evidence". The other corpora's
  evidence is still returned. If *every* selected corpus faults, the whole
  `retrieve` raises (so the service can surface a 5xx, not an abstain) — a retrieval
  outage is not an answer.
- **Merge & dedup** (`merge_evidence`):
  - Iterate corpora in ascending `corpus_id` order, evidence in input order →
    deterministic.
  - Dedup key = stable `evidence_id` (kept as-is; first occurrence wins).
  - Cross-corpus same-content folding: two Evidence sharing
    `(text_hash, document_id, document_version)` but *different* `evidence_id`
    collapse to the higher `authority_level` (tie → corpus order). The loser's
    `corpus_id` is still recorded in `corpora_used` (source attribution preserved),
    but only one primary Evidence is emitted.
  - **Different `document_version` is NOT folded** — same text, different version
    stays as distinct Evidence.
  - `corpora_used` = the set of `corpus_id` of every emitted *and* folded Evidence,
    in ascending order. `insufficient_corpora` = routed corpora that returned zero
    evidence and did not fault.

### Chat service (`services/chat_service.py`)

```python
ChatService.answer_multi_corpus(
    query: str, ctx: SecurityContext, *, corpus_ids: list[str] | None = None
) -> AnswerEnvelope
```

- When `corpus_ids` is `None`, route via `CorpusRouter.route(query, ctx, registry,
  limit=2)` and use the selected `candidates`. Otherwise restrict to the explicitly
  requested (and still discoverable) `corpus_ids`. A requested-but-undiscoverable
  corpus fails closed (never silently dropped into retrieval).
- Run multi-corpus retrieval, then **single-pass** synthesis (`build_answer_envelope`
  with the merged evidence; no judge, no iteration). `corpora_used` on the envelope
  is set from `MultiCorpusResult.corpora_used`.
- If the merged evidence is empty and there are no faults → `conservative_refusal`
  (the existing abstain lock). If there is at least one fault → raise
  (backend outage ≠ answer). If there is evidence → build normally.

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
6. **Fault semantics** — when one corpus's retrieval raises, its fault is captured
   in `faults` and the other corpus's evidence is still returned; a *total* fault
   raises (never becomes an abstain).
7. **`corpora_used` truthful** — only corpora that actually contributed Evidence
   appear; routed-but-empty corpora do not.
8. **No regression** — E-011→E-020, E-015 and `tests/baseline/` all stay green;
   `ruff`, `ruff format`, `mypy src/agentic_rag_enterprise` clean.

## Quality gates

- `ruff check src/agentic_rag_enterprise/corpus/router.py src/agentic_rag_enterprise/retrieval/multi_corpus.py`
- `ruff format --check .`
- `uv run mypy src/agentic_rag_enterprise`
- `pytest tests/unit/corpus tests/unit/retrieval tests/security/test_multi_corpus_isolation.py tests/integration/test_e016_multi_corpus_pipeline.py tests/baseline`
