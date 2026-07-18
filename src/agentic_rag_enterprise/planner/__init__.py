"""E-017 Controlled Planner — pure control plane (build plan §13).

This package is the **contract-only** half of Milestone 5: the Typed Planner data
models, the §13.2 binding grammar, the static DAG Validator, and the Planner
structured-output repair. It contains no executor, no `StepResult`, no retriever / Tool
call — an illegal plan is rejected before any execution.

See :mod:`agentic_rag_enterprise.planner.models`,
:mod:`agentic_rag_enterprise.planner.binding`,
:mod:`agentic_rag_enterprise.planner.validator`, and
:mod:`agentic_rag_enterprise.planner.repair`.
"""

from agentic_rag_enterprise.planner.binding import (
    BindingExpression,
    BindingKind,
    BindingSyntaxError,
)
from agentic_rag_enterprise.planner.models import (
    OutputSchemaId,
    PlanStep,
    PlanValidationResult,
    PlanViolation,
    PlanViolationCode,
    QueryPlan,
    StepDependency,
    StepType,
)
from agentic_rag_enterprise.planner.repair import (
    PlanRepairExhaustedError,
    parse_plan,
)
from agentic_rag_enterprise.planner.validator import PlanValidator

__all__ = [
    "BindingExpression",
    "BindingKind",
    "BindingSyntaxError",
    "OutputSchemaId",
    "PlanStep",
    "PlanValidationResult",
    "PlanViolation",
    "PlanViolationCode",
    "QueryPlan",
    "StepDependency",
    "StepType",
    "PlanRepairExhaustedError",
    "parse_plan",
    "PlanValidator",
]
