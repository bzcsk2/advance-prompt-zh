# Issue E-018 — Controlled DAG Executor + dependent multi-hop

**Milestone:** M5 — Controlled Planner and dependent multi-hop (`E-017 -> E-018`)
**Status:** contract frozen / implementation pending (acceptance of this doc unlocks `executor.py`, result models, budget, tool registry, and tests)
**Baseline:** `0e81ac0` (M5 / E-017 CLOSED / ACCEPTED)
**Build plan refs:** §13.2 (Planner DAG), §13.4 (DAG execution), §13.5 (Planner 不得决定权限), §9.1 / §9.2 (Capability + Corpus Registry), §12.x (retrieval security envelope).
**Depends on:** E-017 `QueryPlan` / `PlanStep` / `StepDependency` / `BindingExpression` / `PlanValidator` (frozen, ACCEPTED at `398f059`/`0e81ac0`).

---

## 1. Scope and non-goals

### In scope (Executor execution plane)

- `StepResult` (frozen, validated, single terminal state per step).
- ready-step parallel scheduling (one topological layer at a time).
- required / optional dependency semantics (scheduling + binding).
- `input_bindings` / `query_template` resolution against completed upstream outputs.
- per-step timeout (`PlanStep.timeout_seconds`) → `timed_out`.
- atomic shared Tool-Call budget (`QueryPlan.max_tool_calls`, `AtomicToolBudget`).
- at most one retry per step (initial + 1 retry).
- failure degradation matrix.
- final execution report (`PlanExecutionResult`).

### Non-goals (deferred / forbidden)

- **No change to E-017 `QueryPlan` semantics** unless an *unexecutable hard gap* is found
  during implementation (none is known at freeze time). If one surfaces, it is raised as a
  contract amendment, not a silent model change.
- **No temporal / authority / conflict arbitration** (explicitly out of M5).
- **No production-grade distributed task scheduling** — a single-process, in-memory
  scheduler is sufficient; no queues, no workers, no durable DAG state machine.
- **No infinite repair / retry** — at most one structured repair (E-017) and at most one
  retry (this issue).
- **No Planner or Tool may read client-supplied tenant / user / role** — the
  `SecurityContext` is injected only by the Executor from the trusted gateway/request
  boundary (mirrors the E-014 rule: client body never asserts `tenant_id` / `is_admin`).
- **No write operations** — only the §13.2 read-only `step_type`s and the M4-enabled
  capabilities (`vector_search`, `document_reader`) are executable; `sql`/`api`/`graph`
  are rejected by E-017 and must never be dispatched.
- **No dynamic step creation** — the Executor runs exactly the steps declared in the
  accepted `QueryPlan`; it never synthesizes, forks, or re-plans.

## 2. `StepResult` state machine

Status is a frozen enum (`StepStatus`), **not** a free string:

```text
pending            # scheduled, not yet run
running            # an attempt is in flight
succeeded          # terminal; outputs available
failed             # terminal; backend / non-retryable fault
timed_out          # terminal; step deadline elapsed
skipped_dependency # terminal; a required upstream did not succeed
budget_exhausted   # terminal; budget reserve failed before launch
```

Invariants (all enforced; violation is a programming error, not a runtime option):

- **Exactly one terminal result per step.** Once a step reaches any terminal status
  (`succeeded` / `failed` / `timed_out` / `skipped_dependency` / `budget_exhausted`) its
  `StepResult` is frozen and cannot change.
- `StepResult` is **immutable** (frozen model).
- Only `succeeded` may carry normal `outputs`; a `failed`/`timed_out` step MUST NOT fabricate
  empty outputs and report `succeeded`.
- A required upstream that did not `succeed` forces every downstream to
  `skipped_dependency` (zero Tool calls).
- An optional upstream failure does **not** block the downstream; the downstream runs, but
  the failed optional binding is delivered as a missing sentinel / omitted field (never as
  error text injected into the query).
- Error detail is split into two channels:
  - `detail` — internal audit text (`Field(exclude=True, repr=False)`, mirrors E-017
    `PlanViolation.detail`);
  - `message` — user-safe text that never contains corpus / tenant / user names.

```python
class StepResult(BaseModel):
    model_config = ConfigDict(frozen=True)
    step_id: str
    status: StepStatus
    outputs: dict[str, object] = Field(default_factory=dict)  # only meaningful on succeeded
    evidence_ids: tuple[str, ...] = Field(default_factory=tuple)
    error_code: str | None = None        # e.g. "retrieval_backend_error", "binding_error"
    message: str = ""                    # USER-SAFE
    detail: str = Field(default="", exclude=True, repr=False)  # internal only
    attempts: int = 0                    # 1 = initial, 2 = initial + 1 retry
    tool_calls_consumed: int = 0         # attempts that actually launched a Tool
```

## 3. Required vs optional dependency

E-017 `PlanStep` already carries `depends_on_step_ids` (hard) and
`optional_depends_on_step_ids` (optional). E-018 freezes semantics:

- **required dependencies**: a step becomes *ready* only after **all** of its required
  upstreams are `succeeded`. A required upstream in any non-`succeeded` terminal state →
  the step is `skipped_dependency` (no execution).
- **optional dependencies**: a step becomes *ready* only after **all** of its dependencies
  (required **and** optional) have reached a terminal state. **Decision: wait for all
  dependencies to be terminal before running** — this avoids non-determinism from a late
  optional result arriving after the step already launched. (Optional success/failure is
  known before launch, so there is no liveness cost.)
- **optional upstream succeeded** → its declared output may be bound.
- **optional upstream failed / timed_out / skipped** → the corresponding binding is
  delivered as a **missing sentinel** (or the binding field is omitted entirely); the
  error text is **never** interpolated into the `query` / `query_template`.
- **required binding missing** (e.g. required upstream not succeeded) → the step MUST NOT
  execute (`skipped_dependency`).
- **optional binding missing** → whether the step may still run is decided per binding
  **field** via the code-side output schema: a field marked optional in the schema tolerates
  absence; a required field without a value blocks execution. The Tool never guesses.

## 4. Binding resolution

`PlanStep` supports `query`, `query_template`, and `input_bindings`. The Executor freezes:

- Binding reads **only** registered outputs of **completed** (terminal, `succeeded`)
  upstream steps. No attribute access beyond the declared `output_field`, no index
  expressions, no function calls, no string evaluation / `eval` / Jinja / Python.
- `facts.<id>.value` source & lifecycle: the value comes from `QueryPlan.required_facts`
  (a planning-time constant), resolved by the Executor before launch; it is a static
  literal, never recomputed at runtime and never merged with retrieval output.
- Template substitution (`{{step_id.field}}`) happens **before** the Tool call, producing a
  plain-text `query`. Bound values are text-escaped and length-limited.
- Missing **required** binding → step does not execute (see §3).
- Bound values undergo **type validation** against the declared output field type.
- Step output must pass the code-side schema registered under `output_schema_id`
  (`entity` / `spec` / `comparison` / `intermediate`). A mismatch is a **non-retryable
  plan / programming error**, not a backend fault.
- Binding failure or output-schema validation failure → `failed` with
  `error_code="binding_error"` / `"output_schema_error"`; **not** retried.

The existing `planner/binding.py` (`BindingExpression.parse`,
`BindingExpression.parse_template_placeholder`) is reused; the Executor adds the
*safe-substitution* + *type/ schema validation* layer on top.

## 5. Parallel scheduling & determinism

- Steps are grouped into **topological layers**; every *ready* step in a layer may run in
  parallel (bounded by an injected concurrency limit; default = number of ready steps).
- The ready queue uses a **stable order** — original plan step order (then `step_id` tie-break).
- Completion order of parallel steps MUST NOT affect the final `StepResult` ordering.
- The final `PlanExecutionResult.steps` is emitted in **original plan / topological order**.
- A step is scheduled **exactly once** (guarded by the terminal-state transition).
- The Executor MUST NOT add steps not declared in the accepted plan.
- An **illegal plan is rejected before the Executor starts** (re-validated against
  `PlanValidator.validate`); execution count for an illegal plan is **zero Tools**.

## 6. Atomic budget

The global budget is `QueryPlan.max_tool_calls`; each step bids `PlanStep.max_tool_calls`
(used by E-017 static pre-validation). E-018 introduces an independent
`AtomicToolBudget`:

```python
class AtomicToolBudget:
    def __init__(self, total: int) -> None: ...
    def reserve(self, n: int = 1) -> bool:
        """Atomically decrement if `n` available; return False (no launch) otherwise.
        Thread-safe; no read-then-write race that would let two parallel steps overspend."""
    def consume(self, n: int = 1) -> None: ...   # record actual attempt count
```

Frozen rules (the §6 race surface):

- **Every actual Tool call, including a retry, consumes exactly one budget unit** via
  `reserve(1)` *before* launch.
- Reserve is **atomic**; a parallel sibling cannot read a stale balance and both overspend.
- Reserve failure → the Tool MUST NOT start; the step is `budget_exhausted`.
- A call that **timed out but already launched** still counts (reserve happened pre-launch).
- A call that was **cancelled but already launched** a Tool still counts.
- `validation`, `binding`, and `scheduling` are **not** Tool Calls.
- `tool_calls_used` in the final report == the sum of all actual launched attempts across
  all steps (initial + retries that actually ran).
- **No refunds** — a retry consumes a fresh unit; refunding would enable double-spend under
  concurrency.
- Steps MUST NOT keep their own counters; all accounting goes through `AtomicToolBudget`.

## 7. Timeout & cancellation

Distinct causes the Executor must separate:

- **Tool backend timeout** — the Tool's own I/O deadline.
- **Executor step deadline** — `PlanStep.timeout_seconds`, enforced by the Executor wrapper.
- **Scheduler-level cancellation** — e.g. parent required-upstream failure cascading,
  or budget exhaustion.
- **Plain backend failure** — `RetrievalBackendError` / `ConnectionError` / `TimeoutError`.

Rules:

- Each step runs under `PlanStep.timeout_seconds` as the Executor deadline.
- On deadline elapse → status `timed_out`.
- If the runtime cannot truly abort the underlying call, the **late completion is discarded**
  and MUST NOT overwrite the already-terminal `timed_out` result (immutable terminal state).
- Whether `timed_out` is retryable is fixed in the §8 matrix (timeout is **retryable once**,
  like a transient infra fault — but a retry that also times out is terminal).
- A step `timed_out` MUST NOT auto-cancel an unrelated parallel sibling (no shared dependency).
- A required downstream of a `timed_out` step is `skipped_dependency`; an optional downstream
  continues per §3.

## 8. Retry matrix

"One retry" means **at most 2 attempts per step: initial + 1 retry.** Only transient
infrastructure faults are retryable:

**Retryable (exactly one retry):**

- `RetrievalBackendError` (the M4 explicit backend fault type)
- `ConnectionError`
- `TimeoutError`
- other explicitly registered transient infra errors (a frozen set in `executor.py`)

**Never retry (terminal `failed`, no retry):**

- permission / binding errors: `CorpusNotDiscoverableError`, `TenantBindingError`,
  `ParentAuthorizationError`, `EmptyAuthorizationScopeError`
- `PlanViolationCode`-class schema / plan errors (binding_error, output_schema_error)
- budget exhaustion (`budget_exhausted`)
- `ValueError` / `TypeError` / `KeyError` (programming errors)
- unknown capability / write operation (already rejected by E-017; if it reaches here, fail closed)
- cancellation

Retry mechanics:

- A retry **reserves a fresh Tool-Call budget unit** (§6) before launching.
- `StepResult.attempts` reflects the true attempt count (1 or 2).
- A non-retryable error on attempt 1 → terminal `failed`, no second attempt.
- Programming errors (`ValueError`/`TypeError`/`KeyError`) propagate as their real type and
  are **never** relabelled as a backend fault or as a partial result.

## 9. Failure degradation matrix

| Situation | Downstream / whole-execution behavior |
| --- | --- |
| required dependency `failed` | downstream `skipped_dependency` (zero Tool calls) |
| required dependency `timed_out` | downstream `skipped_dependency` |
| optional dependency `failed` / `timed_out` | downstream continues per §3 (binding delivered as missing sentinel) |
| independent parallel step `failed` | other independent steps continue |
| security / binding failure | **entire execution fails closed immediately** (no partial result) |
| partial backend failure with usable results | return a **degraded** `PlanExecutionResult` (`degraded=True`, `limitations` listed) |
| no usable result at all | raise a typed `PlanExecutionError` (never a fabricated complete answer) |
| budget exhausted | no new Tool launches; un-started steps marked `budget_exhausted` |
| Planner / Schema bug | propagate in original type; never伪装成 backend failure |
| `timed_out` late completion | discarded; terminal `timed_out` preserved |

"Fail closed" for security/binding means: the moment any step raises a
`CorpusNotDiscoverableError` / `TenantBindingError` / `ParentAuthorizationError` /
`EmptyAuthorizationScopeError`, the Executor stops and raises a typed error — it does not
degrade to a partial answer and does not surface the denied corpus/tenant name.

## 10. Tool / Executor interface

Minimal protocol; the Executor depends on a `Tool` abstraction, **not** directly on
`SecureRetriever`:

```python
class TypedStepOutput(BaseModel):
    model_config = ConfigDict(frozen=True)
    outputs: dict[str, object]
    evidence_ids: tuple[str, ...] = ()
    schema_id: OutputSchemaId

class Tool(Protocol):
    def execute_step(
        self,
        step: PlanStep,
        resolved_inputs: Mapping[str, object],
        ctx: SecurityContext,
    ) -> TypedStepOutput: ...

class ToolRegistry(Protocol):
    def get(self, step_type: str, capability_id: str) -> Tool: ...
```

Rules:

- The `SecurityContext` is **injected only by the Executor**; a Tool MUST NOT derive
  tenant / user / role from `step` or `query`.
- Tools are looked up by `step_type + capability_id` in a `ToolRegistry`. An unregistered
  combination → fail closed (`error_code="tool_not_registered"`).
- The `CorpusConfig` a Tool retrieves from is obtained **only** via `registry.get(corpus_id,
  ctx)` (the E-017 fail-closed truth source). The Tool never receives a raw corpus map.
- Tool output is validated against the `output_schema_id` schema **before** it becomes a
  `StepResult` (§4). Validation failure → `failed`, non-retryable.
- For M5 the only registered Tool is a `RetrieverTool` wrapping
  `SecureRetriever.retrieve_evidence` (the M4/M3 retrieval surface). It returns
  `TypedStepOutput` with `evidence_ids`; a `RetrievalBackendError` is the only fault it
  surfaces as retryable — all security/binding faults propagate in their original type.

## 11. Acceptance matrix (execution test plan)

1. Two independent steps execute **truly in parallel** (shared wall-clock < sequential).
2. Diamond DAG: each step executes **exactly once**.
3. Step 1's extracted entity is **correctly bound** into Step 2's query/template.
4. A `failed` required upstream → downstream runs **zero Tool calls**.
5. A `failed` optional upstream → downstream **still executes** (binding missing sentinel).
6. A `timed_out` step's result is **not overwritten** by a late completion.
7. Retry happens **exactly once** on a retryable fault, and **consumes two budget units**.
8. A programming error (`ValueError`/`TypeError`/`KeyError`) is **not retried**.
9. With concurrency limit / budget = 1, **at most one Tool** is ever in flight.
10. Retry **and** parallelism together still **never overspend** the budget.
11. An unauthorized Corpus fails closed **before or during** execution (no Tool call against it).
12. A security error is **not** downgraded to a partial `StepResult`.
13. An illegal plan executes **zero Tools** (re-validated, rejected pre-launch).
14. Final `StepResult` ordering is **deterministic** (plan / topological order).
15. `PlanExecutionResult.tool_calls_used` **equals** the real launched-attempt count.
16. User-visible errors, `str()` / `repr()` and serialized report **never leak** corpus /
    tenant / user names (`detail` is `exclude=True, repr=False`).
17. The Executor **never dynamically creates** a new step.
18. Single-corpus Fast Path (E-012), M3 iteration (E-019/E-020) and M4 multi-corpus
    (E-015/E-016) full regressions are **unaffected**.

## 12. Quality gates (implementation)

- `ruff check src tests`, `ruff format --check .`, `uv run mypy src/agentic_rag_enterprise`
  clean.
- New `tests/unit/planner/test_executor.py`, `tests/unit/planner/test_atomic_budget.py`,
  `tests/integration/test_e018_executor_pipeline.py` (covering the §11 matrix).
- Full `pytest` (baseline / unit / security / integration / evals) green.
- Architecture test: `executor` package still does not import any *untrusted* planner
  output as authority for tenant/corpus; `SecurityContext` is always injected by the
  Executor, never read from a step/query.

---

### Contract-only commit boundary

This freeze commits only `docs/issue-e018-contract.md` + `AGENTS.md`. Implementation
(`executor.py`, `StepResult`/`PlanExecutionResult`/`AtomicToolBudget`/`ToolRegistry`,
`RetrieverTool`, and the test paths) opens **after** this contract is accepted.
