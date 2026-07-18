"""Unit tests for the E-017 static DAG Validator (planner/validator.py).

Mirrors build plan §13.3 + the M5 exit gate. The validator performs NO retrieval — it
only consults the fail-closed registry for corpus authorization, so an illegal plan is
rejected with zero Tools executed.
"""

from __future__ import annotations


from agentic_rag_enterprise.corpus.registry import InMemoryCorpusRegistry
from agentic_rag_enterprise.domain.security import SecurityContext
from agentic_rag_enterprise.judge.models import RequiredFact
from agentic_rag_enterprise.planner.models import (
    PlanStep,
    PlanViolationCode,
    QueryPlan,
)
from agentic_rag_enterprise.planner.validator import PlanValidator


def _ctx(tenant_id: str = "local", allowed: list[str] | None = None) -> SecurityContext:
    return SecurityContext(
        request_id="r",
        session_id="s",
        tenant_id=tenant_id,
        user_id="u",
        policy_version="1.0",
        allowed_corpus_ids=allowed,
    )


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


def _happy_plan() -> QueryPlan:
    """The §13.4 dependent 2-hop example (valid against the M4 fixtures)."""
    return QueryPlan(
        plan_id="plan_001",
        task_type="dependent_multi_hop",
        max_iterations=1,
        max_tool_calls=4,
        required_facts=(
            RequiredFact(fact_id="F1", description="server id"),
            RequiredFact(fact_id="F2", description="specs", depends_on_fact_ids=("F1",)),
        ),
        steps=(
            _step(
                step_id="find_server",
                target_corpus_ids=("engineering_wiki",),
                query="Project X production server identifier",
                output_schema_id="entity",
                max_tool_calls=2,
            ),
            _step(
                step_id="find_specs",
                depends_on_step_ids=("find_server",),
                target_corpus_ids=("product_docs",),
                query_template="server {{find_server.server_id}} hardware specifications",
                input_bindings={"server_id": "steps.find_server.outputs.server_id"},
                output_schema_id="spec",
                max_tool_calls=2,
            ),
        ),
    )


def test_happy_path_accepted() -> None:
    res = PlanValidator.validate(_happy_plan(), _ctx(), InMemoryCorpusRegistry())
    assert res.accepted is True
    assert res.violations == ()
    assert res.policy_violation_attempt is False


def test_duplicate_step_id_rejected() -> None:
    p = _happy_plan()
    p = p.model_copy(deep=True)
    p.steps[1].__dict__  # no-op to satisfy frozen; rebuild instead
    p = QueryPlan(
        plan_id="p",
        task_type="t",
        max_iterations=1,
        max_tool_calls=4,
        steps=(_step(step_id="find_server"), _step(step_id="find_server")),
    )
    res = PlanValidator.validate(p, _ctx(), InMemoryCorpusRegistry())
    codes = {v.code for v in res.violations}
    assert PlanViolationCode.DUPLICATE_STEP_ID in codes
    assert res.accepted is False


def test_unknown_dependency_rejected() -> None:
    p = _happy_plan().model_copy(deep=True)
    steps = list(p.steps)
    steps[1] = steps[1].model_copy(update={"depends_on_step_ids": ("ghost",)})
    p = p.model_copy(update={"steps": tuple(steps)})
    res = PlanValidator.validate(p, _ctx(), InMemoryCorpusRegistry())
    assert PlanViolationCode.UNKNOWN_DEPENDENCY in {v.code for v in res.violations}


def test_self_dependency_is_cycle() -> None:
    p = QueryPlan(
        plan_id="p",
        task_type="t",
        max_iterations=1,
        max_tool_calls=3,
        steps=(_step(step_id="s1", depends_on_step_ids=("s1",)),),
    )
    res = PlanValidator.validate(p, _ctx(), InMemoryCorpusRegistry())
    assert PlanViolationCode.CYCLE_DETECTED in {v.code for v in res.violations}


def test_two_step_mutual_cycle_rejected() -> None:
    p = QueryPlan(
        plan_id="p",
        task_type="t",
        max_iterations=1,
        max_tool_calls=4,
        steps=(
            _step(step_id="a", depends_on_step_ids=("b",)),
            _step(step_id="b", depends_on_step_ids=("a",)),
        ),
    )
    res = PlanValidator.validate(p, _ctx(), InMemoryCorpusRegistry())
    assert PlanViolationCode.CYCLE_DETECTED in {v.code for v in res.violations}
    assert res.accepted is False


def test_three_step_loop_rejected() -> None:
    p = QueryPlan(
        plan_id="p",
        task_type="t",
        max_iterations=1,
        max_tool_calls=6,
        steps=(
            _step(step_id="a", depends_on_step_ids=("c",)),
            _step(step_id="b", depends_on_step_ids=("a",)),
            _step(step_id="c", depends_on_step_ids=("b",)),
        ),
    )
    res = PlanValidator.validate(p, _ctx(), InMemoryCorpusRegistry())
    assert PlanViolationCode.CYCLE_DETECTED in {v.code for v in res.violations}


def test_optional_dependency_does_not_create_cycle() -> None:
    p = QueryPlan(
        plan_id="p",
        task_type="t",
        max_iterations=1,
        max_tool_calls=4,
        steps=(
            _step(step_id="a", optional_depends_on_step_ids=("b",)),
            _step(step_id="b"),
        ),
    )
    res = PlanValidator.validate(p, _ctx(), InMemoryCorpusRegistry())
    assert PlanViolationCode.CYCLE_DETECTED not in {v.code for v in res.violations}
    assert res.accepted is True


def test_unauthorized_corpus_rejected_named_not_leaked() -> None:
    p = _happy_plan().model_copy(deep=True)
    steps = list(p.steps)
    steps[0] = steps[0].model_copy(update={"target_corpus_ids": ("secret_corpus",)})
    p = p.model_copy(update={"steps": tuple(steps)})
    res = PlanValidator.validate(p, _ctx(), InMemoryCorpusRegistry())
    assert res.accepted is False
    assert res.policy_violation_attempt is True
    viol = next(v for v in res.violations if v.code == PlanViolationCode.CORPUS_NOT_AUTHORIZED)
    assert "secret_corpus" not in viol.message  # user-safe
    assert viol.detail == "secret_corpus"  # internal only, exclude=True
    dumped = viol.model_dump(mode="json")
    assert "secret_corpus" not in dumped
    assert "detail" not in dumped


def test_tenant_isolation_blocks_other_tenant_corpus() -> None:
    # A different tenant cannot see the 'local' fixtures.
    res = PlanValidator.validate(_happy_plan(), _ctx(tenant_id="other"), InMemoryCorpusRegistry())
    assert res.accepted is False
    assert res.policy_violation_attempt is True


def test_unknown_capability_rejected() -> None:
    p = _happy_plan().model_copy(deep=True)
    steps = list(p.steps)
    steps[0] = steps[0].model_copy(update={"capability_id": "sql"})
    p = p.model_copy(update={"steps": tuple(steps)})
    res = PlanValidator.validate(p, _ctx(), InMemoryCorpusRegistry())
    assert PlanViolationCode.CAPABILITY_NOT_ALLOWED in {v.code for v in res.violations}
    # write capability is also flagged as WRITE_OPERATION
    assert PlanViolationCode.WRITE_OPERATION in {v.code for v in res.violations}


def test_step_budget_exceeds_global_rejected() -> None:
    p = _happy_plan().model_copy(deep=True)
    steps = list(p.steps)
    steps[0] = steps[0].model_copy(update={"max_tool_calls": 99})
    p = p.model_copy(update={"steps": tuple(steps)})
    res = PlanValidator.validate(p, _ctx(), InMemoryCorpusRegistry())
    assert PlanViolationCode.STEP_BUDGET_EXCEEDS_GLOBAL in {v.code for v in res.violations}


def test_total_budget_exceeds_global_rejected() -> None:
    p = _happy_plan().model_copy(deep=True)
    # global=4, each step bid 3 -> sum 6 > 4
    steps = [s.model_copy(update={"max_tool_calls": 3}) for s in p.steps]
    p = p.model_copy(update={"steps": tuple(steps)})
    res = PlanValidator.validate(p, _ctx(), InMemoryCorpusRegistry())
    assert PlanViolationCode.TOTAL_BUDGET_EXCEEDS_GLOBAL in {v.code for v in res.violations}
    assert PlanViolationCode.STEP_BUDGET_EXCEEDS_GLOBAL not in {v.code for v in res.violations}


def test_empty_query_rejected() -> None:
    p = _happy_plan().model_copy(deep=True)
    steps = list(p.steps)
    steps[0] = steps[0].model_copy(update={"query": None, "query_template": None})
    p = p.model_copy(update={"steps": tuple(steps)})
    res = PlanValidator.validate(p, _ctx(), InMemoryCorpusRegistry())
    assert PlanViolationCode.EMPTY_QUERY in {v.code for v in res.violations}


def test_invalid_binding_unknown_step_rejected() -> None:
    p = _happy_plan().model_copy(deep=True)
    steps = list(p.steps)
    steps[1] = steps[1].model_copy(
        update={"input_bindings": {"server_id": "steps.nope.outputs.server_id"}}
    )
    p = p.model_copy(update={"steps": tuple(steps)})
    res = PlanValidator.validate(p, _ctx(), InMemoryCorpusRegistry())
    assert PlanViolationCode.INVALID_BINDING in {v.code for v in res.violations}


def test_invalid_binding_unknown_fact_rejected() -> None:
    p = _happy_plan().model_copy(deep=True)
    steps = list(p.steps)
    steps[0] = steps[0].model_copy(update={"input_bindings": {"x": "facts.NOPE.value"}})
    p = p.model_copy(update={"steps": tuple(steps)})
    res = PlanValidator.validate(p, _ctx(), InMemoryCorpusRegistry())
    assert PlanViolationCode.INVALID_BINDING in {v.code for v in res.violations}


def test_invalid_template_placeholder_rejected() -> None:
    p = _happy_plan().model_copy(deep=True)
    steps = list(p.steps)
    steps[1] = steps[1].model_copy(update={"query_template": "run {{ghost.x}} now"})
    p = p.model_copy(update={"steps": tuple(steps)})
    res = PlanValidator.validate(p, _ctx(), InMemoryCorpusRegistry())
    assert PlanViolationCode.INVALID_BINDING in {v.code for v in res.violations}


def test_collect_all_reports_every_defect() -> None:
    # A plan that is simultaneously cyclic, over-budget, unauthorized, and bad-binding.
    p = QueryPlan(
        plan_id="p",
        task_type="t",
        max_iterations=1,
        max_tool_calls=1,
        steps=(
            _step(
                step_id="a",
                depends_on_step_ids=("b",),
                target_corpus_ids=("secret_corpus",),
                query_template="x {{ghost.y}}",
                max_tool_calls=5,
            ),
            _step(step_id="b", depends_on_step_ids=("a",), max_tool_calls=5),
        ),
    )
    res = PlanValidator.validate(p, _ctx(), InMemoryCorpusRegistry())
    codes = {v.code for v in res.violations}
    assert PlanViolationCode.CYCLE_DETECTED in codes
    assert PlanViolationCode.CORPUS_NOT_AUTHORIZED in codes
    assert PlanViolationCode.STEP_BUDGET_EXCEEDS_GLOBAL in codes
    assert PlanViolationCode.INVALID_BINDING in codes
    assert res.policy_violation_attempt is True
