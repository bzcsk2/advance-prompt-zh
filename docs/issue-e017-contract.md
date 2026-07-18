# Issue E-017 — Typed `QueryPlan` / `PlanStep` Contract + DAG Validator

**Milestone:** M5 — Controlled Planner and dependent multi-hop (`E-017 -> E-018`)
**Status:** in progress
**Baseline:** `033c8e2` (M4 / E-016 CLOSED / ACCEPTED)
**Build plan refs:** §13.1–§13.5 (Query Complexity Routing & Planner DAG), §9.1 (Capability Catalog), §9.2 (Corpus Registry).

---

## 1. Goal

Introduce the **pure control plane** of the Milestone 5 Controlled Planner:

- Frozen, validated Typed Planner data contract (`QueryPlan`, `PlanStep`,
  `StepDependency`, `BindingExpression`, `PlanValidationResult`).
- A `PlanValidator` that statically rejects, **before any execution**, every plan that:
  - has duplicate `step_id`s;
  - references a non-existent dependency / binding source;
  - contains a cycle (not a DAG);
  - targets a Corpus the caller cannot see (fail-closed via `registry.get`);
  - requests a capability outside the allowlist;
  - violates the static tool-call budget (each step ≤ global **and**
    `sum(steps) ≤ global`);
  - has empty `query`/`query_template`;
  - carries a write operation;
  - declares `input_bindings` that are not well-formed upstream references.
- A structural-output `repair` path that allows **at most one** structured repair of a
  malformed Planner output, then degrades (raised to the caller, which degrades to
  controlled cross-corpus retrieval in E-018).

E-017 is **contract-only**: there is no Executor, no `StepResult`, no scheduling, no
`SecureRetriever` / Tool call, no retry, no budget allocator. The exit-gate guarantee
"illegal DAG runs zero Tools" is therefore structurally guaranteed — the validator
returns `accepted=False` and there is no code path in E-017 that performs retrieval.

## 2. Non-goals (deferred to E-018)

- `StepResult`, parallel scheduling, dependency binding at execution time, per-step
  timeout, atomic shared budget allocator, exactly-one retry, failure degradation,
  Planner structured-output *generation* (an LLM Planner). Those belong to E-018.
- Temporal-conflict arbitration; production task scheduling.

## 3. Authoritative §13 references (verbatim intent)

- §13.2 `step_type` ∈ {`retrieve`, `extract`, `compare`, `synthesize_intermediate`};
  unknown type must be rejected (no free-form Tool fallback).
- §13.2 `input_bindings` values are **only** `steps.<step_id>.outputs.<field>` or
  `facts.<fact_id>.value`; no arbitrary expressions. `query_template` uses *only*
  pre-parsed placeholders, never Jinja/Python; bound values are plain-text.
- §13.2 `output_schema_id` must reference a code-side registered schema (accepted set is
  frozen for E-017: `"entity"`, `"spec"`, `"comparison"`, `"intermediate"`); a model
  may not invent arbitrary JSON Schema.
- §13.3 validator checklist: step_id unique; all deps exist; acyclic; corpus in
  caller-visible range; capability in allowlist; each-step budget ≤ global; query
  non-empty; `input_bindings` reference legal upstream; no write operation.
- §13.3 repair: first failure → exactly one structured repair; second failure → degrade.
  No infinite repair loop.
- §13.5 Planner may only pick from the *already filtered* Corpus / Capability list.
  An output targeting a non-discoverable Corpus → validator rejects →
  `policy_violation_attempt` recorded → **not executed**; the offending Corpus name must
  NOT be surfaced to the ordinary user.

## 4. Data contract (`src/agentic_rag_enterprise/planner/models.py`)

All models are `frozen=True` + `ConfigDict(frozen=True)` and carry field validators so
two fields can never contradict each other (mirrors the E-012/E-013 validated-model
approach). Lists are stored as `tuple` for immutability.

```python
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field

StepType = Literal["retrieve", "extract", "compare", "synthesize_intermediate"]
# Frozen, registered output schema ids (no model-invented schema).
OutputSchemaId = Literal["entity", "spec", "comparison", "intermediate"]

class PlanStep(BaseModel):
    model_config = ConfigDict(frozen=True)
    step_id: str
    step_type: StepType
    description: str
    required_fact_ids: tuple[str, ...] = Field(default_factory=tuple)
    depends_on_step_ids: tuple[str, ...] = Field(default_factory=tuple)   # hard deps
    optional_depends_on_step_ids: tuple[str, ...] = Field(default_factory=tuple)  # §13.2 optional
    target_corpus_ids: tuple[str, ...] = Field(default_factory=tuple)
    capability_id: str = "vector_search"
    query: str | None = None
    query_template: str | None = None
    input_bindings: dict[str, str] = Field(default_factory=dict)  # field -> binding expr
    output_schema_id: OutputSchemaId
    max_tool_calls: int = 2
    timeout_seconds: int = 30

class QueryPlan(BaseModel):
    model_config = ConfigDict(frozen=True)
    plan_id: str
    task_type: str
    required_facts: tuple[RequiredFact, ...] = Field(default_factory=tuple)  # reused from judge.models
    steps: tuple[PlanStep, ...]
    max_iterations: int
    max_tool_calls: int   # GLOBAL budget, forwarded from the query-complexity router
```

### `StepDependency` (validator-side edge type)

```python
class StepDependency(BaseModel):
    model_config = ConfigDict(frozen=True)
    upstream_step_id: str
    downstream_step_id: str
    optional: bool
```

The validator builds the edge set from each step's `depends_on_step_ids`
(`optional=False`) and `optional_depends_on_step_ids` (`optional=True`). A step id that
appears in *both* a hard and an optional list is a contradiction → `DUPLICATE_DEPENDENCY`
violation. A self-dependency (`step_id in depends_on_step_ids`) → `CYCLE_DETECTED`.

### `PlanViolationCode` (frozen enum)

```python
class PlanViolationCode(str, Enum):
    DUPLICATE_STEP_ID
    UNKNOWN_DEPENDENCY
    DUPLICATE_DEPENDENCY
    CYCLE_DETECTED
    CORPUS_NOT_AUTHORIZED
    CAPABILITY_NOT_ALLOWED
    STEP_BUDGET_EXCEEDS_GLOBAL
    TOTAL_BUDGET_EXCEEDS_GLOBAL
    EMPTY_QUERY
    INVALID_BINDING
    UNKNOWN_STEP_TYPE
    UNKNOWN_OUTPUT_SCHEMA
    WRITE_OPERATION
    POLICY_VIOLATION
    REPAIR_EXHAUSTED
```

### `PlanViolation` (frozen)

```python
class PlanViolation(BaseModel):
    model_config = ConfigDict(frozen=True)
    code: PlanViolationCode
    message: str                      # USER-SAFE (never contains corpus/tenant names)
    detail: str = Field(default="", exclude=True)  # internal-only, not serialized to user
    step_id: str | None = None
```

The `message` field is the only user-facing text and MUST be generic (e.g.
"plan references a corpus that is not authorized for this request"). The offending
corpus id / tenant is recorded only in `detail` (`Field(exclude=True)`), mirroring the
E-009 `denied_reasons` redaction pattern — §13.5 forbids leaking the denied Corpus name.

### `PlanValidationResult` (frozen)

```python
class PlanValidationResult(BaseModel):
    model_config = ConfigDict(frozen=True)
    accepted: bool
    violations: tuple[PlanViolation, ...] = Field(default_factory=tuple)
    # policy_violation_attempt is set True when a corpus-capability authorization
    # violation was detected (§13.5 telemetry); never surfaced to the user.
    policy_violation_attempt: bool = False

    @property
    def is_accepted(self) -> bool: ...  # == accepted
```

## 5. Binding grammar (`src/agentic_rag_enterprise/planner/binding.py`)

```python
class BindingKind(str, Enum):
    STEP_OUTPUT = "step_output"
    FACT_VALUE = "fact_value"

class BindingExpression(BaseModel):
    model_config = ConfigDict(frozen=True)
    raw: str
    kind: BindingKind
    step_id: str | None = None      # when kind == STEP_OUTPUT
    output_field: str | None = None # when kind == STEP_OUTPUT
    fact_id: str | None = None      # when kind == FACT_VALUE

    @classmethod
    def parse(cls, raw: str) -> "BindingExpression": ...   # raises BindingSyntaxError
```

Grammar (§13.2):

- `steps.<step_id>.outputs.<field>` → `STEP_OUTPUT`. `<field>` must be a non-empty
  identifier. `<step_id>` is free-form text but is later checked against the plan's
  declared steps by the validator.
- `facts.<fact_id>.value` → `FACT_VALUE`. `<fact_id>` is checked against
  `QueryPlan.required_facts`.
- Anything else (empty string, arbitrary expression, `{{...}}` text, Jinja, Python) →
  `BindingSyntaxError`.

`parse` is pure (no regex backtracking, deterministic). The `query_template` placeholders
(`{{step_id.field}}`) are parsed by a separate `parse_template_placeholder` that yields a
`BindingExpression` of kind `STEP_OUTPUT` and is validated against declared dependencies.

## 6. Validator (`src/agentic_rag_enterprise/planner/validator.py`)

```python
class PlanValidator:
    @staticmethod
    def validate(
        plan: QueryPlan,
        ctx: SecurityContext,
        registry: CorpusRegistry,
    ) -> PlanValidationResult: ...
```

Order of checks (each appends violations; the validator is **collect-all**, never early
return, so a single call reports every defect):

1. **Step id uniqueness** — duplicate `step_id` → `DUPLICATE_STEP_ID`.
2. **Unknown / duplicate dependency** — every id in `depends_on_step_ids` /
   `optional_depends_on_step_ids` must exist among the plan's step ids; otherwise
   `UNKNOWN_DEPENDENCY`. Intersection of the two lists → `DUPLICATE_DEPENDENCY`. A step id
   present in its own hard-dep list → `CYCLE_DETECTED`.
3. **DAG / cycle detection** — build the edge set (hard + optional edges). Run Kahn /
   DFS topological sort; any residual edge → `CYCLE_DETECTED`.
4. **Corpus authorization (fail-closed, §13.5)** — for every `target_corpus_ids` entry,
   call `registry.get(corpus_id, ctx)`. `CorpusNotDiscoverableError` →
   `CORPUS_NOT_AUTHORIZED` (user-safe `message`, id in `detail`, `exclude=True`), and
   `policy_violation_attempt=True`. No corpus name reaches the user; the corpus is simply
   not in an accepted plan. **The validator never calls any retriever / Tool** — this is
   what makes "illegal DAG → zero Tool" structurally true.
5. **Capability allowlist** — `CapabilityCatalog.supports(capability_id)` must be `True`;
   otherwise `CAPABILITY_NOT_ALLOWED`. (Covers the §13.5 "Planner may only pick from the
   filtered capability list"; `sql`/`api`/`graph` are reserved-but-disabled → not
   allowed.)
6. **Static budget pre-validation** (confirmed scope):
   - each step `max_tool_calls ≤ plan.max_tool_calls` → else `STEP_BUDGET_EXCEEDS_GLOBAL`;
   - `sum(step.max_tool_calls for step in steps) ≤ plan.max_tool_calls` →
     `TOTAL_BUDGET_EXCEEDS_GLOBAL`.
   This is the strong static guarantee the M5 exit gate requires ("总预算在执行前可静态
   校验"; parallel + the E-018 retry must never overspend — retry accounting is the
   E-018 atomic allocator's job, but the static sum leaves headroom *only* if E-018
   enforces it; E-017 just freezes the contract).
7. **Query non-empty** — `retrieve`/`extract`/`compare`/`synthesize_intermediate` steps
   must have a non-empty `query` **or** `query_template`; else `EMPTY_QUERY`.
8. **`input_bindings` well-formedness** — each value must `BindingExpression.parse`
   cleanly (`INVALID_BINDING` on `BindingSyntaxError`). A `STEP_OUTPUT` binding's
   `step_id` must be a declared dependency (hard *or* optional) of the current step, and
   `output_field` must be a non-empty identifier. A `FACT_VALUE` binding's `fact_id` must
   exist in `plan.required_facts`. Each `query_template` placeholder must likewise
   reference a declared dependency step.
9. **No write operation** — `step_type` is restricted to the §13.2 read-only Literal, so a
   write type is rejected as `UNKNOWN_STEP_TYPE`; `output_schema_id` must be a registered
   `OutputSchemaId` else `UNKNOWN_OUTPUT_SCHEMA`. (There is no write capability in
   `CapabilityCatalog.enabled`, so `WRITE_OPERATION` is a defensive alias but the Literal
   already excludes it.)

`accepted = (len(violations) == 0)`.

## 7. Repair (`src/agentic_rag_enterprise/planner/repair.py`)

```python
class PlanRepairExhaustedError(Exception): ...

def parse_plan(raw: dict | str, *, repair_fn: Callable[[dict], dict]) -> QueryPlan: ...
```

- `raw` is the untrusted Planner output (dict or JSON string). It is parsed into a
  `QueryPlan` via the frozen model (pydantic validation).
- On the **first** `ValidationError` (schema error), `repair_fn(raw)` is called exactly
  once to produce a corrected dict, which is re-parsed. A `ValidationError` on the
  repaired dict raises `PlanRepairExhaustedError` (no second repair — §13.3 "禁止无限
  修复"). A `repair_fn` that itself raises is propagated (never silently swallowed).
- `parse_plan` does **not** run `PlanValidator.validate` (that is the caller's job after
  parsing). It only guarantees *typed* round-trip. The at-most-one-repair invariant is
  enforced by the single `repair_fn` call count.

## 8. Reuse (no change)

- `judge/models.py:RequiredFact` — `QueryPlan.required_facts`.
- `corpus/registry.py` — `CorpusRegistry` protocol + `InMemoryCorpusRegistry`;
  `registry.get(corpus_id, ctx)` is the fail-closed authorization primitive.
- `corpus/capability_registry.py` — `CapabilityCatalog.supports`.
- `domain/security.py` — `SecurityContext`.
- `retrieval/models.py` — `CorpusNotDiscoverableError` (re-raised by the validator as a
  violation, never leaked to the user).

## 9. Acceptance tests (exit gate)

`tests/integration/test_e017_planner_contract.py` + `tests/unit/planner/*`:

| Criterion | Test |
| --- | --- |
| Illegal DAG runs **zero Tools** | No executor exists; `validate` returns `accepted=False` and the test asserts `registry.get` is the only external call (counted), `retriever` is never imported/touched. |
| Cycle rejected | A 2-step mutual-dependency plan → `CYCLE_DETECTED` (and a 3-step loop). |
| Missing/unknown binding rejected | `input_bindings` referencing a non-declared `step_id` / `fact_id` → `INVALID_BINDING`. |
| Unauthorized Corpus never in accepted plan | A plan targeting a non-discoverable corpus → `CORPUS_NOT_AUTHORIZED`, `policy_violation_attempt=True`, `accepted=False`; assert the violation `message` contains no corpus name and `detail` is `exclude=True`. |
| Total budget statically rejected | `sum(steps) > plan.max_tool_calls` → `TOTAL_BUDGET_EXCEEDS_GLOBAL`; also a single step over global. |
| Malformed output repaired at most once | A bad `raw` is repaired by an instrumented `repair_fn`; assert it is invoked exactly once and the result is accepted; a second bad (post-repair) `raw` raises `PlanRepairExhaustedError`. |
| Unknown capability rejected | `capability_id="sql"` → `CAPABILITY_NOT_ALLOWED`. |
| No write operation | a `step_type` outside the Literal → `UNKNOWN_STEP_TYPE`. |
| Happy path | the §13.4 `find_server` → `find_specs` dependent 2-hop plan validates `accepted=True` (authz against the M4 fixtures). |

## 10. Quality gates

- `ruff check src tests`, `ruff format --check .`, `uv run mypy src/agentic_rag_enterprise`
  clean.
- Full `pytest` (baseline / unit / security / integration / evals) green; E-017 adds
  `tests/unit/planner/*` and `tests/integration/test_e017_planner_contract.py`.
- No import of `retrieval/retriever.py`, `SecureRetriever`, or any `*_service` from the
  `planner` package (verified by an architecture test asserting `planner` does not import
  the executor/retrieval-execution surface).
