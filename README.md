# agentic-rag-enterprise

Enterprise-grade Agentic RAG runtime inspired by Gemini Enterprise Agent Platform's Agentic RAG architecture.

This repository is a Python-first implementation blueprint for building dependable, auditable, multi-corpus RAG systems. It focuses on **planning, corpus routing, iterative retrieval, sufficient-context checking, grounded synthesis, traceability, and evaluation** rather than a simple "vector search + LLM answer" demo.

> Current repository name on GitHub may still be `advance-prompt-zh`. Rename it in GitHub Settings to `agentic-rag-enterprise` after this migration.

## Why this project exists

Traditional RAG often fails in enterprise settings because the retrieved context can be relevant but still incomplete. This project treats RAG as an agent runtime problem:

- the system must plan what facts are required;
- route subqueries to the right corpora;
- retrieve, rerank, and consolidate evidence;
- judge whether the current context is sufficient;
- continue searching when evidence is incomplete;
- abstain when the answer cannot be grounded;
- preserve a full trace for review, replay, and evaluation.

## Target architecture

```text
User Query
   ↓
Root Orchestrator
   ├── Query Normalizer
   ├── Planner Agent
   ├── Corpus Router
   ├── Query Rewriter
   ├── Search Fanout
   ├── Evidence Store
   ├── Sufficient Context Agent
   ├── Iteration Controller
   └── Grounded Synthesis Agent
   ↓
Auditable Answer + Citations + Trace
```

## Development status

This repository is **not** a scaffold. It implements the enterprise Agentic RAG runtime
end-to-end for a local (in-process) **Internal MVP / pre-Research-MVP** runtime. The
current milestone is
**M7 — Runtime hardening**, and the following issues are **CLOSED / ACCEPTED**:

- **E-011** — Evidence snapshot store + required dedup (current-policy re-auth)
- **E-012** — Single-corpus Fast Path + one-pass sufficiency decision
- **E-013** — AnswerEnvelope, citation rendering, claim verification, conservative refusal
- **E-014** — Shared chat service, synchronous `POST /v1/chat`, minimal Gradio adapter
- **E-015 / E-016** — Multi-corpus registry, permission-aware router, cross-corpus merge/dedup
- **E-017** — Typed `QueryPlan` / `PlanStep` contract + DAG Validator
- **E-018** — Controlled Executor + dependent multi-hop (`RetrieverTool`)
- **E-019 / E-020** — Required-Fact Coverage judge + explicit state transitions; bounded gap
  retrieval + `no_new_evidence` stop policy
- **E-021** — Temporal scope, source authority & conflict resolution
- **E-022** — Reconciler + purge + index migration + rollback
- **E-023** — Persistent checkpoint + re-authorization on resume (cross-process recoverable,
  fail-closed ACL re-auth, `completed` idempotent, `aborted` non-resumable)

**Open within M7:** **E-024** — Health/readiness, persistent cancellation, backup/restore +
runbooks. See `docs/issue-e024-contract.md`.

### Delivered capabilities (Internal MVP)

- Corpus / capability registry with permission-safe discoverability.
- Idempotent ingestion job + active-version protocol (Metadata DB = control-plane source
  of truth).
- Parent-child chunking + hybrid (dense+sparse) retrieval with second-authorization.
- Single-pass Fast Path and bounded, gap-driven iteration loop with deterministic
  coverage judge and conservative refusal (fail-closed: dependency faults never become
  "no answer").
- Planner DAG validation + controlled executor with atomic tool budget (multi-hop).
- Temporal filtering + conflict resolution feeding explicit answer states
  (`sufficient` / `contradicted` / `partial` / `abstained`).
- Persistent run checkpoint + resume re-authorization (E-023).
- Reconciler for orphan purge / data-plane rebuild / index rollback (E-022).

### Known limitations (by design, pre-M9)

- Local SQLite (Metadata DB) + in-process/local Qdrant only — **no Postgres, no Qdrant
  Server, no SSO, no distributed scheduler** (those are M9).
- Reconciler / checkpoint are single-process; a restart that loses a non-file-backed
  temp DB loses in-flight checkpoints (the default container uses a stable file).
- Local backup is **unencrypted** (no approved encryption dependency); see E-024.
- A basic liveness `/health` endpoint exists; the dependency-aware `/ready` probe and the
  finalized E-024 health/cancellation/backup-restore surface are **not yet implemented**
  (contract open at `docs/issue-e024-contract.md`).

## Core capabilities
- **Corpus Registry**: structured descriptions, ownership, metadata, and ACL boundaries for each enterprise knowledge source.
- **Planner / Router**: decomposes user questions and routes subqueries to the right corpora.
- **Hybrid Retrieval**: dense + sparse retrieval with metadata filtering and reranking hooks.
- **Sufficient Context Agent**: judges whether retrieved evidence is enough to answer; emits missing facts and next queries.
- **Iterative Retrieval Loop**: bounded loop with max iterations, max tool calls, visited-query cache, and stop reasons.
- **Grounded Synthesis**: answers only from evidence, with citation maps and abstention support.
- **Evaluation Harness**: tests retrieval quality, answer faithfulness, sufficiency detection, and refusal behavior.
- **Observability**: records plans, tool calls, evidence sets, sufficiency decisions, cost, latency, and final claims.

## Repository layout

```text
agentic-rag-enterprise/
  src/agentic_rag_enterprise/
    agents/              # planner, router, SCA, synthesis
    api/                 # FastAPI service
    evals/               # benchmark cases and metrics
    graph/               # LangGraph-compatible runtime state and orchestration
    ingestion/           # parsing, chunking, indexing
    observability/       # traces, evidence store, citation map
    retrieval/           # corpus registry, retriever, reranker
    security/            # ACL filter and prompt-injection checks
  docs/
    architecture.md
  examples/
    corpora.yaml
  tests/
```

## Quick start

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
make test
```

Run the API service:

```bash
uvicorn agentic_rag_enterprise.api.main:app --reload
```

## Design principle

RAG is not just retrieval. Enterprise Agentic RAG needs runtime governance: state, traces, permission boundaries, evidence sufficiency, bounded iteration, and evaluation feedback loops.

## License

MIT
