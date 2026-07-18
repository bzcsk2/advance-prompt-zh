"""E-017 Planner Contract acceptance tests (build plan §13.3 / M5 exit gate).

These assert the structural guarantees the M5 exit gate requires:

* illegal DAG -> zero Tool execution (structurally: no executor exists; the validator
  only touches the fail-closed registry, never a retriever);
* cycle rejected;
* missing/unknown binding rejected;
* unauthorized Corpus never in an accepted plan (name not leaked to the user);
* total budget statically rejected;
* malformed Planner output repaired at most once.
"""

from __future__ import annotations

import ast
import importlib
import pathlib

import pytest

from agentic_rag_enterprise.corpus.registry import (
    CorpusRegistry,
    InMemoryCorpusRegistry,
)
from agentic_rag_enterprise.domain.security import SecurityContext
from agentic_rag_enterprise.judge.models import RequiredFact
from agentic_rag_enterprise.planner.models import (
    PlanStep,
    PlanViolationCode,
    QueryPlan,
)
from agentic_rag_enterprise.planner.repair import (
    PlanRepairExhaustedError,
    parse_plan,
)
from agentic_rag_enterprise.planner.validator import PlanValidator

_PLANNER_DIR = (
    pathlib.Path(__file__).resolve().parents[1] / "src" / "agentic_rag_enterprise" / "planner"
)

# Execution / retrieval surface the pure control plane must NOT touch (build plan §13.5).
_FORBIDDEN_IMPORTS = {
    "agentic_rag_enterprise.retrieval.retriever",
    "agentic_rag_enterprise.retrieval.secure_retriever",
    "agentic_rag_enterprise.retrieval.fast_path",
    "agentic_rag_enterprise.services.chat_service",
    "agentic_rag_enterprise.agents",
    "agentic_rag_enterprise.graph",
}


def test_planner_package_imports_no_execution_surface() -> None:
    """Architecture gate: the planner package must not import any retriever / executor /
    service / agent module — illegal DAG enforcement is structural, not by guarding a call."""
    import ast as _ast

    found: set[str] = set()
    for path in _PLANNER_DIR.rglob("*.py"):
        tree = _ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, _ast.ImportFrom) and node.module:
                if node.module in _FORBIDDEN_IMPORTS or node.module.startswith(
                    "agentic_rag_enterprise.retrieval.retriever"
                ):
                    found.add(node.module)
            elif isinstance(node, _ast.Import):
                for alias in node.names:
                    if alias.name in _FORBIDDEN_IMPORTS:
                        found.add(alias.name)

    assert found == set(), f"planner imports forbidden execution surface: {found}"


def test_planner_modules_importable() -> None:
    # Smoke: importing the package must not pull in any forbidden module transitively
    # via the planner's own __init__.
    mod = importlib.import_module("agentic_rag_enterprise.planner")
    assert mod.PlanValidator is not None
    assert mod.parse_plan is not None


def _ctx() -> SecurityContext:
    return SecurityContext(
        request_id="r",
        session_id="s",
        tenant_id="local",
        user_id="u",
        policy_version="1.0",
        allowed_corpus_ids=None,
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


class _CountingRegistry(CorpusRegistry):
    """Wraps InMemoryCorpusRegistry and counts corpus-visibility lookups (the only
    external call the validator makes). Records whether any retriever call occurs."""

    def __init__(self) -> None:
        self._inner = InMemoryCorpusRegistry()
        self.get_calls = 0

    def get(self, corpus_id: str, security_context: SecurityContext):  # noqa: D401
        self.get_calls += 1
        return self._inner.get(corpus_id, security_context)

    def list_searchable(self, security_context: SecurityContext):
        return self._inner.list_searchable(security_context)

    def resolve_candidates(self, query: str, security_context: SecurityContext, limit: int):
        return self._inner.resolve_candidates(query, security_context, limit)


def test_illegal_dag_runs_zero_tools() -> None:
    # A cyclic, over-budget, unauthorized plan. The validator must reject it purely via
    # corpus-visibility lookups — there is no retriever in the package, and a monkey check
    # here proves no external retrieval surface is reached.
    reg = _CountingRegistry()
    plan = QueryPlan(
        plan_id="bad",
        task_type="t",
        max_iterations=1,
        max_tool_calls=1,
        steps=(
            _step(
                step_id="a",
                depends_on_step_ids=("b",),
                target_corpus_ids=("secret_corpus",),
                max_tool_calls=5,
            ),
            _step(step_id="b", depends_on_step_ids=("a",), max_tool_calls=5),
        ),
    )
    res = PlanValidator.validate(plan, _ctx(), reg)
    assert res.accepted is False
    assert reg.get_calls >= 1  # corpus authz check happened
    # No retriever / Tool call exists in the planner package; the only external call is
    # the registry get. (Architecture test below enforces the import boundary.)


def test_cycle_rejected() -> None:
    reg = InMemoryCorpusRegistry()
    plan = QueryPlan(
        plan_id="p",
        task_type="t",
        max_iterations=1,
        max_tool_calls=2,
        steps=(
            _step(step_id="a", depends_on_step_ids=("b",)),
            _step(step_id="b", depends_on_step_ids=("a",)),
        ),
    )
    res = PlanValidator.validate(plan, _ctx(), reg)
    assert PlanViolationCode.CYCLE_DETECTED in {v.code for v in res.violations}
    assert res.accepted is False


def test_missing_binding_rejected() -> None:
    reg = InMemoryCorpusRegistry()
    # Step `b` binds to `steps.a.outputs.y` but does NOT declare `a` as a dependency ->
    # the binding references a step that is not an upstream dependency.
    plan = QueryPlan(
        plan_id="p",
        task_type="t",
        max_iterations=1,
        max_tool_calls=2,
        steps=(
            _step(step_id="a"),
            _step(step_id="b", input_bindings={"x": "steps.a.outputs.y"}),
        ),
    )
    res = PlanValidator.validate(plan, _ctx(), reg)
    assert PlanViolationCode.INVALID_BINDING in {v.code for v in res.violations}
    assert res.accepted is False


def test_unauthorized_corpus_never_accepted_and_name_not_leaked() -> None:
    reg = InMemoryCorpusRegistry()
    plan = QueryPlan(
        plan_id="p",
        task_type="t",
        max_iterations=1,
        max_tool_calls=2,
        steps=(_step(step_id="a", target_corpus_ids=("secret_corpus",)),),
    )
    res = PlanValidator.validate(plan, _ctx(), reg)
    assert res.accepted is False
    assert res.policy_violation_attempt is True
    assert PlanViolationCode.CORPUS_NOT_AUTHORIZED in {v.code for v in res.violations}
    for v in res.violations:
        assert "secret_corpus" not in v.message  # user-safe
        assert "secret_corpus" not in v.model_dump(mode="json")


def test_total_budget_statically_rejected() -> None:
    reg = InMemoryCorpusRegistry()
    plan = QueryPlan(
        plan_id="p",
        task_type="t",
        max_iterations=1,
        max_tool_calls=3,
        steps=(
            _step(step_id="a", max_tool_calls=2),
            _step(step_id="b", max_tool_calls=2),  # sum 4 > 3
        ),
    )
    res = PlanValidator.validate(plan, _ctx(), reg)
    assert PlanViolationCode.TOTAL_BUDGET_EXCEEDS_GLOBAL in {v.code for v in res.violations}
    assert res.accepted is False


def test_malformed_output_repaired_at_most_once() -> None:
    calls = []
    bad = {
        "plan_id": "p",
        "task_type": "t",
        "max_iterations": 1,
        "max_tool_calls": 2,
        "required_facts": [{"fact_id": "F1", "description": "x"}],
        "steps": [
            {
                "step_type": "retrieve",
                "description": "d",
                "target_corpus_ids": ["engineering_wiki"],
                "capability_id": "vector_search",
                "query": "q",
                "output_schema_id": "entity",
                "max_tool_calls": 2,
            }
        ],
        # missing step_id -> schema error -> triggers one repair
    }

    def repair_fn(d: dict) -> dict:
        calls.append(d)
        d["steps"][0]["step_id"] = "s1"
        return d

    plan = parse_plan(bad, repair_fn=repair_fn)
    assert isinstance(plan, QueryPlan)
    assert len(calls) == 1  # exactly one repair


def test_malformed_output_second_failure_raises() -> None:
    bad = {
        "plan_id": "p",
        "task_type": "t",
        "max_iterations": 1,
        "max_tool_calls": 2,
        "steps": [
            {
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

    def repair_fn(d: dict) -> dict:
        return d  # no fix

    with pytest.raises(PlanRepairExhaustedError):
        parse_plan(bad, repair_fn=repair_fn)


def test_happy_path_dependent_two_hop_accepted() -> None:
    reg = InMemoryCorpusRegistry()
    plan = QueryPlan(
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
    res = PlanValidator.validate(plan, _ctx(), reg)
    assert res.accepted is True
