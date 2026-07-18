"""Unit tests for the E-017 Typed Planner data contract (planner/models.py)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from agentic_rag_enterprise.judge.models import RequiredFact
from agentic_rag_enterprise.planner.models import (
    OutputSchemaId,
    PlanStep,
    PlanValidationResult,
    PlanViolation,
    PlanViolationCode,
    QueryPlan,
    StepType,
)


def _step(**kw) -> PlanStep:
    base = dict(
        step_id="s1",
        step_type="retrieve",
        description="d",
        target_corpus_ids=("engineering_wiki",),
        query="q",
        output_schema_id="entity",
    )
    base.update(kw)
    return PlanStep(**base)


def test_models_are_frozen() -> None:
    plan = QueryPlan(
        plan_id="p",
        task_type="t",
        max_iterations=1,
        max_tool_calls=2,
        steps=(_step(),),
    )
    with pytest.raises(ValidationError):
        plan.steps[0].query = "mutated"  # type: ignore[misc]


def test_unknown_step_type_rejected() -> None:
    with pytest.raises(ValidationError):
        _step(step_type="write_to_db")  # not in the §13.2 Literal


def test_unknown_output_schema_rejected() -> None:
    with pytest.raises(ValidationError):
        _step(output_schema_id="arbitrary_json_schema")  # model may not invent schema


def test_optional_depends_field_exists() -> None:
    step = _step(optional_depends_on_step_ids=("s0",))
    assert step.optional_depends_on_step_ids == ("s0",)
    assert step.all_dependency_ids() == ("s0",)
    assert "s0" in step.all_dependency_ids()


def test_plan_violation_detail_is_excluded() -> None:
    v = PlanViolation.user_safe(
        PlanViolationCode.CORPUS_NOT_AUTHORIZED,
        "plan targets a corpus that is not authorized for this request",
        detail="secret_corpus",
        step_id="s1",
    )
    dumped = v.model_dump(mode="json")
    assert "detail" not in dumped
    assert dumped["message"] != "secret_corpus"


def test_validation_result_accepted_flag() -> None:
    ok = PlanValidationResult(accepted=True)
    assert ok.is_accepted is True
    bad = PlanValidationResult(
        accepted=False,
        violations=(PlanViolation.user_safe(PlanViolationCode.CYCLE_DETECTED, "cyclic"),),
    )
    assert bad.is_accepted is False
    assert bad.policy_violation_attempt is False


def test_required_fact_reused_from_judge_models() -> None:
    rf = RequiredFact(fact_id="F1", description="x", depends_on_fact_ids=("F0",))
    plan = QueryPlan(
        plan_id="p",
        task_type="t",
        max_iterations=1,
        max_tool_calls=2,
        required_facts=(rf,),
        steps=(_step(),),
    )
    assert plan.required_fact_ids() == ("F1",)
    assert plan.step_ids() == ("s1",)


def test_step_type_and_schema_literals_exist() -> None:
    assert set(StepType.__args__) == {  # type: ignore[attr-defined]
        "retrieve",
        "extract",
        "compare",
        "synthesize_intermediate",
    }
    assert set(OutputSchemaId.__args__) == {  # type: ignore[attr-defined]
        "entity",
        "spec",
        "comparison",
        "intermediate",
    }
