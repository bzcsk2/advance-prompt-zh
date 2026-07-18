# Issue E-018 — Controlled DAG Executor + dependent multi-hop

**Milestone:** M5 — Controlled Planner and dependent multi-hop (`E-017 -> E-018`)
**Status:** contract amended (P1-1..P1-5 + P2 from the `5d02d99` re-audit) / implementation still pending — acceptance of this amended doc unlocks `executor.py`, result models, budget, tool registry, and tests
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

### 2a. `PlanExecutionResult` and the "usable result" definition (P1-4 amendment)

The final report is frozen as:

```python
class PlanExecutionResult(BaseModel):
    model_config = ConfigDict(frozen=True)
    plan_id: str
    accepted: bool                       # plan passed pre-execution validation
    executed: bool                      # at least one Tool launch was attempted
    degraded: bool                      # partial success -> True (see usable-result rule)
    steps: tuple[StepResult, ...]       # in deterministic plan / topological order
    tool_calls_used: int                # == sum of StepResult.tool_calls_consumed
    evidence_ids: tuple[str, ...]       # union of all succeeded steps' evidence_ids
    limitations: tuple[str, ...] = ()   # human-readable degradation notes (user-safe)
    error_code: str | None = None       # set when the whole execution fails closed
    message: str = ""                   # USER-SAFE (never corpus/tenant/user names)
    detail: str = Field(default="", exclude=True, repr=False)  # internal only
```

Invariants:

- `tool_calls_used` == `sum(s.tool_calls_consumed for s in steps)` (single source of truth =
  the `AtomicToolBudget` used count).
- `steps` ordering is deterministic (§5): original plan step order with topological tie-break.
- `evidence_ids` = the union of `evidence_ids` across `succeeded` steps only.
- `limitations` entries are **user-safe** (no corpus/tenant name); internal specifics live in
  `detail`.

**"Usable result" definition (frozen):**

- A result is **usable** iff **at least one `succeeded` step produced Evidence**
  (`evidence_ids` non-empty). Intermediate-only success with no Evidence is *not* usable on
  its own for the answer path.
- **Degraded** (`degraded=True`) iff: not all steps succeeded, yet at least one `succeeded`
  step produced Evidence. The report is returned with `limitations` listing the failed/
  skipped steps (user-safe). Example: the first hop succeeds (Entity found) but the second
  hop fails or is skipped → **degraded**, not raised.
- **Fail closed (raise `PlanExecutionError`)** iff **zero** steps produced usable Evidence
  (no `succeeded` step with Evidence). This includes: all steps failed/timed_out/skipped,
  or only non-Evidence intermediate steps succeeded. A fabricated complete answer is never
  returned.
- **Sink-step rule:** there is no special "only the sink step counts" exception — usability
  is Evidence-based, so any hop that yields Evidence makes the overall result usable; the
  final merge (in the caller, e.g. `answer_multi_corpus` / answer builder) decides which
  Evidence feeds the answer. The Executor only reports `evidence_ids` + `degraded`.

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
- **`facts.<id>.value` source & lifecycle (P1-1 amendment).** The E-017 `RequiredFact`
  model has only `fact_id` / `description` / `required` / `depends_on_fact_ids` — there is
  **no** `value` field. To keep E-018 within E-017's frozen contract (no silent E-017
  amendment), `facts.<id>.value` is **frozen to mean `RequiredFact.description`**: the
  planning-time textual statement of the fact. The Executor resolves it from
  `QueryPlan.required_facts` before launch; it is a static literal, never recomputed at
  runtime and never merged with retrieval output. The binding value is the `description`
  string (plain text, length-limited). No separate `fact_values` map and no new E-017 field
  are introduced. (If a future milestone needs richer fact values, that is an E-017 contract
  amendment, not an E-018 executor concern.)
- Template substitution (`{{step_id.field}}`) happens **before** the Tool call, producing a
  plain-text `query`. Bound values are text-escaped and length-limited.
- Missing **required** binding → step does not execute (see §3).
- Bound values undergo **type validation** against the declared output field type.
- Step output must pass the code-side schema registered under `output_schema_id`
  (`entity` / `spec` / `comparison` / `intermediate`). A mismatch is a **non-retryable
  plan / programming error**, not a backend fault.
- **Data binding failure** (Planner-data problem, NOT a security event) → `failed` with
  `error_code="binding_error"` / `"output_schema_error"`; **not** retried. Per §3/§4a a
  missing *required* input field blocks that single step (which may cascade as
  `skipped_dependency` to its required downstream), but **other independent steps continue**
  — this is a local step failure, not a whole-execution abort.
- **Security / corpus binding failure** is a distinct class: `TenantBindingError`,
  `CorpusNotDiscoverableError`, `ParentAuthorizationError`, `EmptyAuthorizationScopeError`
  raised while resolving a corpus / tenant / evidence binding. This is a **security event**
  (§9) and triggers immediate whole-execution fail-closed — never a partial result. The two
  classes share the word "binding" only incidentally; their handling is mutually exclusive
  and determined by exception **type**, not by the `binding_error` code.

The existing `planner/binding.py` (`BindingExpression.parse`,
`BindingExpression.parse_template_placeholder`) is reused; the Executor adds the
*safe-substitution* + *type/ schema validation* layer on top.

### 4a. `ToolSpec` and input-schema-driven optional binding (P1-2 amendment)

To decide whether a **missing optional binding** blocks or allows a step, the Executor
needs per-field input typing. This is supplied by a `ToolSpec` returned alongside the
`Tool` from the `ToolRegistry` — it is an **execution-plane** concept (E-018), not part of
the E-017 `QueryPlan`, so it requires no E-017 model change:

```python
from pydantic import BaseModel

class ToolSpec(BaseModel):
    model_config = ConfigDict(frozen=True)
    step_type: str
    capability_id: str
    input_model: type[BaseModel]        # validates resolved_inputs; fields carry
                                         # Optional[...] / required to decide missingness
    output_model: type[BaseModel]       # validates TypedStepOutput against output_schema_id
    retryable_errors: frozenset[type[Exception]]  # e.g. {RetrievalBackendError,
                                                  #        ConnectionError, TimeoutError}
```

- The `ToolRegistry.get(step_type, capability_id)` returns `(Tool, ToolSpec)` (or a
  `ToolRegistration` carrying both).
- **Optional-binding missingness rule (frozen):** before launch the Executor builds
  `resolved_inputs` from completed-upstream outputs + `facts.<id>.value`. It then validates
  `resolved_inputs` against `ToolSpec.input_model`. A field declared `Optional[...]` (or
  explicitly nullable) **tolerates absence** → the step runs with that field `None` /
  omitted. A **required** input field that is missing (because a required upstream did not
  succeed, or a required `input_binding` resolved to nothing) → the step **does not execute**
  and is `skipped_dependency` / `failed` per §3/§4. The Tool never guesses; the decision is
  driven entirely by `ToolSpec.input_model`.
- `output_model` is the code-side schema registered under `output_schema_id`
  (`entity` / `spec` / `comparison` / `intermediate`); a mismatch → non-retryable
  `failed` (`error_code="output_schema_error"`).

This removes the earlier undefined "field-level schema" reference: the deciding schema is
now the explicit `ToolSpec.input_model`.

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
    def try_reserve(self, n: int = 1) -> bool:
        """Atomically attempt to spend `n` units. On success: remaining -= n AND
        used += n in ONE locked operation, return True. If insufficient: return False
        and change nothing. No separate `consume` step exists, so there is a single
        accounting transition and no double-count / leak path."""
    def used(self) -> int: ...          # == sum of all successful reservations
    def remaining(self) -> int: ...     # == total - used
```

Frozen rules (the §6 race surface) — **single, unambiguous API:**

- **Every actual Tool call, including a retry, spends exactly one budget unit** via
  `try_reserve(1)` *before* launch. `try_reserve` atomically does `remaining -= 1;
  used += 1` in one lock; there is **no separate `reserve` + `consume`** (that dual API was
  ambiguous and is removed — P2 amendment).
- `try_reserve` returns `False` → the Tool MUST NOT start; the step is `budget_exhausted`.
- A call that **timed out but already launched** still counts (the unit was spent at
  `try_reserve` pre-launch, never refunded).
- A call that was **cancelled but already launched** a Tool still counts.
- `validation`, `binding`, and `scheduling` are **not** Tool Calls and never call
  `try_reserve`.
- `tool_calls_used` in the final report == `AtomicToolBudget.used()` == the sum of all
  actual launched attempts across all steps (initial + retries that actually ran). Single
  source of truth.
- **No refunds** — a retry spends a fresh unit; refunding would enable double-spend under
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
| security / corpus binding failure (`TenantBindingError` / `CorpusNotDiscoverableError` / `ParentAuthorizationError` / `EmptyAuthorizationScopeError`) | **entire execution fails closed immediately** (no partial result) — distinct from the local `binding_error` data failure in §4 |
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

### 10a. `RetrieverTool` Evidence → `entity`/`spec` projection (P1-5 amendment)

`retrieve_evidence` returns `list[SnapshotEvidence]` (rich metadata: `text`,
`corpus_id`, `document_id`, `section_path`, `authority_level`, `retrieval_score`, …). M5
does **not** add an LLM `ExtractTool` — projection is **deterministic and model-free**,
driven by `PlanStep.output_schema_id`:

- **`entity`** → `{"entity_text": <top evidence text>, "corpus_id": <corpus_id>,
  "document_id": <document_id>, "section_path": <section_path>, "authority_level": <int>}`.
  When multiple Evidence exist, the one with the highest `retrieval_score` (tie-break:
  highest `authority_level`, then `evidence_id`) is selected as `entity_text`; all
  `evidence_ids` are still returned.
- **`spec`** → `{"spec_text": <top evidence text>, "corpus_id": <corpus_id>,
  "document_id": <document_id>, "metadata": {authority_level, retrieval_score,
  section_path}}` — same selection rule.
- **`comparison`** → `{"items": [per-evidence {corpus_id, text, authority_level}],
  "evidence_ids": [...]}`.
- **`intermediate`** → `{"texts": [evidence text per hit], "evidence_ids": [...]}`.

The projection is pure mapping over the returned Evidence fields (no generation, no call
into the synthesis model). This is what makes the dependent two-hop real end-to-end:
Step 1 (`output_schema_id="entity"`) yields `entity_text`; the binding
`steps.step1.outputs.entity_text` is substituted into Step 2's `query_template`; Step 2
retrieves against the bound value. No Fake Tool is required for the main path. The soft
binding field name (`entity_text` / `spec_text`) is part of the frozen `ToolSpec.output_model`
so the grammar's `output_field` (`steps.<id>.outputs.<field>`) resolves against a real key.

## 11. Acceptance matrix (execution test plan)

1. Two independent steps execute **truly in parallel** (shared wall-clock < sequential).
2. Diamond DAG: each step executes **exactly once**.
3. Step 1's extracted entity is **correctly bound** into Step 2's query/template.
4. A `failed` required upstream → downstream runs **zero Tool calls**.
5. A `failed` optional upstream → downstream **still executes** (binding missing sentinel).
6. A `timed_out` step's result is **not overwritten** by a late completion.
7. Retry happens **exactly once** on a retryable fault, and **spends two units** via two
   `try_reserve(1)` calls (used count == 2).
8. A programming error (`ValueError`/`TypeError`/`KeyError`) is **not retried**.
9. With concurrency limit / budget = 1, **at most one Tool** is ever in flight.
10. Retry **and** parallelism together still **never overspend** the budget (single
    `try_reserve` transition, no `reserve`+`consume` double-count).
11. An unauthorized Corpus fails closed **before or during** execution (no Tool call against it).
12. A security error is **not** downgraded to a partial `StepResult`.
13. An illegal plan executes **zero Tools** (re-validated, rejected pre-launch).
14. Final `StepResult` ordering is **deterministic** (plan / topological order).
15. `PlanExecutionResult.tool_calls_used` **equals** `AtomicToolBudget.used()` (real launched
    attempts).
16. User-visible errors, `str()` / `repr()` and serialized report **never leak** corpus /
    tenant / user names (`detail` is `exclude=True, repr=False`).
17. The Executor **never dynamically creates** a new step.
18. Single-corpus Fast Path (E-012), M3 iteration (E-019/E-020) and M4 multi-corpus
    (E-015/E-016) full regressions are **unaffected**.
19. `facts.<id>.value` binding resolves to `RequiredFact.description` (no value field exists
    in E-017 `RequiredFact`); the resolved text is plain-text and length-limited.
20. A data `binding_error` (missing required input per `ToolSpec.input_model`) fails only
    that step and lets independent steps continue; a `TenantBindingError` /
    `CorpusNotDiscoverableError` raised mid-execution aborts the **whole** execution.
21. `PlanExecutionResult` is emitted with `degraded=True` + `limitations` when ≥1 step
    produced Evidence but not all succeeded; raises `PlanExecutionError` when **zero** steps
    produced Evidence (no fabricated answer).
22. A two-hop plan whose Step-1 (`entity`) succeeds and Step-2 fails returns a **degraded**
    report carrying Step-1 `evidence_ids` — not a raised error and not a complete answer.
23. `RetrieverTool` projects `list[SnapshotEvidence]` into `entity_text` / `spec_text` via the
    frozen deterministic rule (no LLM), so Step-1 output binds into Step-2 `query_template`
    on the real main path (no Fake Tool).

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
