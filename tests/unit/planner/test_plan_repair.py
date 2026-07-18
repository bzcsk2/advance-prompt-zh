"""Unit tests for the E-017 Planner structured-output repair (planner/repair.py).

Build plan §13.3: a malformed Planner output may be repaired **at most once**; a second
failure degrades (raises PlanRepairExhaustedError). No infinite repair loop.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from agentic_rag_enterprise.judge.models import RequiredFact
from agentic_rag_enterprise.planner.models import PlanStep, QueryPlan
from agentic_rag_enterprise.planner.repair import (
    PlanRepairExhaustedError,
    parse_plan,
)


def _valid_dict() -> dict:
    return {
        "plan_id": "p",
        "task_type": "t",
        "max_iterations": 1,
        "max_tool_calls": 2,
        "required_facts": [{"fact_id": "F1", "description": "x"}],
        "steps": [
            {
                "step_id": "s1",
                "step_type": "retrieve",
                "description": "d",
                "target_corpus_ids": ["engineering_wiki"],
                "capability_id": "vector_search",
                "query": "q",
                "output_schema_id": "entity",
                "max_tool_calls": 2,
            }
        ],
    }


def _step(**kw) -> PlanStep:
    base = dict(
        step_id="s1",
        step_type="retrieve",
        description="d",
        target_corpus_ids=("engineering_wiki",),
        capability_id="vector_search",
        query="q",
        output_schema_id="entity",
        max_tool_calls=2,
    )
    base.update(kw)
    return PlanStep(**base)


def _good_plan() -> QueryPlan:
    return QueryPlan(
        plan_id="p",
        task_type="t",
        max_iterations=1,
        max_tool_calls=2,
        required_facts=(RequiredFact(fact_id="F1", description="x"),),
        steps=(_step(),),
    )


def test_valid_raw_parses_without_repair() -> None:
    calls = []
    plan = parse_plan(_valid_dict(), repair_fn=lambda d: calls.append(d) or d)
    assert isinstance(plan, QueryPlan)
    assert calls == []  # repair never invoked


def test_malformed_raw_repaired_once() -> None:
    calls = []
    bad = _valid_dict()
    del bad["plan_id"]  # missing required field -> schema error

    def repair_fn(d: dict) -> dict:
        calls.append(d)
        d["plan_id"] = "p"  # fix
        return d

    plan = parse_plan(bad, repair_fn=repair_fn)
    assert isinstance(plan, QueryPlan)
    assert len(calls) == 1  # exactly one repair
    assert plan.plan_id == "p"


def test_second_failure_raises_repair_exhausted() -> None:
    bad = _valid_dict()
    del bad["plan_id"]

    def repair_fn(d: dict) -> dict:
        # returns a dict but still missing plan_id -> still invalid
        return d

    with pytest.raises(PlanRepairExhaustedError):
        parse_plan(bad, repair_fn=repair_fn)


def test_repair_fn_invoked_only_once_even_if_multiple_fields_bad() -> None:
    calls = []
    bad = _valid_dict()
    del bad["plan_id"]
    del bad["task_type"]

    def repair_fn(d: dict) -> dict:
        calls.append(d)
        d["plan_id"] = "p"
        d["task_type"] = "t"
        return d

    plan = parse_plan(bad, repair_fn=repair_fn)
    assert len(calls) == 1
    assert plan.plan_id == "p"


def test_repair_fn_raising_propagates() -> None:
    bad = _valid_dict()
    del bad["plan_id"]

    def repair_fn(d: dict) -> dict:
        raise RuntimeError("llm exploded")

    with pytest.raises(RuntimeError):
        parse_plan(bad, repair_fn=repair_fn)


def test_non_dict_non_str_raises_validation_error() -> None:
    with pytest.raises(ValidationError):
        parse_plan(123, repair_fn=lambda d: d)  # type: ignore[arg-type]
