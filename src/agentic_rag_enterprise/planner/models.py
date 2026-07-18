"""E-017 Typed Planner data contract (build plan §13.2 / §13.3 / §13.5).

Pure control plane: frozen, validated models that describe a Planner DAG before any
execution. There is intentionally **no** executor, no `StepResult`, no retriever / Tool
reference here — an illegal plan is rejected by :class:`~agentic_rag_enterprise.planner.
validator.PlanValidator` and nothing in E-017 performs retrieval.

The contract mirrors the E-012 / E-013 validated-model style: every model is frozen and
two fields can never contradict each other. `list` fields are stored as `tuple` so a
validated plan is immutable.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from agentic_rag_enterprise.judge.models import RequiredFact

# README: §13.2 fixed executor types — an unknown `step_type` is rejected, never
# silently promoted to a free-form Tool call (build plan §13.2: "未知类型必须拒绝").
StepType = Literal["retrieve", "extract", "compare", "synthesize_intermediate"]

# Frozen, code-side registered output schema ids. A model may not invent arbitrary
# JSON Schema (build plan §13.2: "output_schema_id 必须引用代码注册表中已存在的 Schema").
OutputSchemaId = Literal["entity", "spec", "comparison", "intermediate"]


class PlanStep(BaseModel):
    """A single DAG node (build plan §13.2)."""

    model_config = ConfigDict(frozen=True)

    step_id: str
    step_type: StepType
    description: str

    required_fact_ids: tuple[str, ...] = Field(default_factory=tuple)
    # Hard dependencies: the step cannot run until these complete successfully.
    depends_on_step_ids: tuple[str, ...] = Field(default_factory=tuple)
    # §13.2 optional dependencies: if an upstream optional dep fails, this step may
    # still continue (the E-018 executor decides; the validator only records optionality).
    optional_depends_on_step_ids: tuple[str, ...] = Field(default_factory=tuple)

    target_corpus_ids: tuple[str, ...] = Field(default_factory=tuple)
    capability_id: str = "vector_search"

    query: str | None = None
    query_template: str | None = None

    # field name -> binding expression string (§13.2 grammar only).
    input_bindings: dict[str, str] = Field(default_factory=dict)

    output_schema_id: OutputSchemaId

    max_tool_calls: int = 2
    timeout_seconds: int = 30

    def all_dependency_ids(self) -> tuple[str, ...]:
        """Hard + optional dependency ids (deduped, order-preserving)."""
        seen: dict[str, None] = {}
        for sid in (*self.depends_on_step_ids, *self.optional_depends_on_step_ids):
            seen.setdefault(sid, None)
        return tuple(seen.keys())


class QueryPlan(BaseModel):
    """A full Planner DAG (build plan §13.2)."""

    model_config = ConfigDict(frozen=True)

    plan_id: str
    task_type: str

    required_facts: tuple[RequiredFact, ...] = Field(default_factory=tuple)
    steps: tuple[PlanStep, ...]

    max_iterations: int
    # GLOBAL tool-call budget, forwarded from the query-complexity router. The
    # static pre-validation (validator) enforces each step ≤ global AND sum ≤ global.
    max_tool_calls: int

    def step_ids(self) -> tuple[str, ...]:
        return tuple(s.step_id for s in self.steps)

    def required_fact_ids(self) -> tuple[str, ...]:
        return tuple(f.fact_id for f in self.required_facts)


class StepDependency(BaseModel):
    """Validator-side typed edge of the DAG (hard or optional)."""

    model_config = ConfigDict(frozen=True)

    upstream_step_id: str
    downstream_step_id: str
    optional: bool


class PlanViolationCode(str, Enum):
    """Every reason a plan can be rejected (build plan §13.3 / §13.5)."""

    DUPLICATE_STEP_ID = "duplicate_step_id"
    UNKNOWN_DEPENDENCY = "unknown_dependency"
    DUPLICATE_DEPENDENCY = "duplicate_dependency"
    CYCLE_DETECTED = "cycle_detected"
    CORPUS_NOT_AUTHORIZED = "corpus_not_authorized"
    CAPABILITY_NOT_ALLOWED = "capability_not_allowed"
    STEP_BUDGET_EXCEEDS_GLOBAL = "step_budget_exceeds_global"
    TOTAL_BUDGET_EXCEEDS_GLOBAL = "total_budget_exceeds_global"
    EMPTY_QUERY = "empty_query"
    INVALID_BINDING = "invalid_binding"
    UNKNOWN_STEP_TYPE = "unknown_step_type"
    UNKNOWN_OUTPUT_SCHEMA = "unknown_output_schema"
    WRITE_OPERATION = "write_operation"
    POLICY_VIOLATION = "policy_violation"
    REPAIR_EXHAUSTED = "repair_exhausted"


class PlanViolation(BaseModel):
    """A single rejected reason.

    `message` is the **user-safe** text and must never contain a corpus / tenant name
    (build plan §13.5: "不要把具体无权限 Corpus 名称反馈给普通用户"). The offending
    identifier is recorded only in `detail`, which is `Field(exclude=True)` so it cannot
    leak through serialization — mirroring the E-009 `denied_reasons` redaction pattern.
    """

    model_config = ConfigDict(frozen=True)

    code: PlanViolationCode
    message: str
    detail: str = Field(default="", exclude=True)
    step_id: str | None = None

    @classmethod
    def user_safe(
        cls,
        code: PlanViolationCode,
        message: str,
        *,
        detail: str = "",
        step_id: str | None = None,
    ) -> "PlanViolation":
        """Construct a violation with a guaranteed generic, user-safe `message`."""
        return cls(code=code, message=message, detail=detail, step_id=step_id)


class PlanValidationResult(BaseModel):
    """Outcome of :meth:`PlanValidator.validate` (build plan §13.3)."""

    model_config = ConfigDict(frozen=True)

    accepted: bool
    violations: tuple[PlanViolation, ...] = Field(default_factory=tuple)
    # §13.5 telemetry: True when a corpus/capability authorization violation was seen.
    # Never surfaced to the user.
    policy_violation_attempt: bool = False

    @property
    def is_accepted(self) -> bool:
        return self.accepted
