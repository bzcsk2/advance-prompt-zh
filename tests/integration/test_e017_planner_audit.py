"""E-017 acceptance re-audit (10 checks, build plan §13 / M5 exit gate).

This file is the *independent* re-verification pass requested before E-018. It probes
edge cases beyond the first implementation tests: negative/zero budgets, capability
spoofing, registry-as-sole-truth-source, log/exception text redaction, and E-018
readiness of the frozen contract.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from agentic_rag_enterprise.corpus.capability_registry import CapabilityCatalog
from agentic_rag_enterprise.corpus.registry import CorpusRegistry, InMemoryCorpusRegistry
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


# ---------------------------------------------------------------------------
# 1. Schema invariants: frozen, duplicate step id, required/optional boundaries, self-dep
# ---------------------------------------------------------------------------


def test_models_frozen_cannot_mutate() -> None:
    p = QueryPlan(plan_id="p", task_type="t", max_iterations=1, max_tool_calls=2, steps=(_step(),))
    with pytest.raises(ValidationError):
        p.steps[0].query = "x"  # type: ignore[misc]


def test_duplicate_step_id_detected() -> None:
    p = QueryPlan(
        plan_id="p",
        task_type="t",
        max_iterations=1,
        max_tool_calls=2,
        steps=(_step(step_id="dup"), _step(step_id="dup")),
    )
    res = PlanValidator.validate(p, _ctx(), InMemoryCorpusRegistry())
    assert PlanViolationCode.DUPLICATE_STEP_ID in {v.code for v in res.violations}


def test_required_and_optional_dependency_are_distinct() -> None:
    # A step cannot list the same upstream in both hard and optional lists.
    p = QueryPlan(
        plan_id="p",
        task_type="t",
        max_iterations=1,
        max_tool_calls=3,
        steps=(
            _step(step_id="a"),
            _step(step_id="b", depends_on_step_ids=("a",), optional_depends_on_step_ids=("a",)),
        ),
    )
    res = PlanValidator.validate(p, _ctx(), InMemoryCorpusRegistry())
    assert PlanViolationCode.DUPLICATE_DEPENDENCY in {v.code for v in res.violations}


def test_optional_dependency_without_hard_is_valid() -> None:
    p = QueryPlan(
        plan_id="p",
        task_type="t",
        max_iterations=1,
        max_tool_calls=4,
        steps=(
            _step(step_id="a", max_tool_calls=2),
            _step(step_id="b", optional_depends_on_step_ids=("a",), max_tool_calls=2),
        ),
    )
    res = PlanValidator.validate(p, _ctx(), InMemoryCorpusRegistry())
    assert res.accepted is True


# ---------------------------------------------------------------------------
# 2. DAG integrity: required AND optional edges both enter cycle + reference checks
# ---------------------------------------------------------------------------


def test_optional_edge_participates_in_cycle_detection() -> None:
    # b optionally depends on a, a hard-depends on b -> still a cycle.
    p = QueryPlan(
        plan_id="p",
        task_type="t",
        max_iterations=1,
        max_tool_calls=4,
        steps=(
            _step(step_id="a", depends_on_step_ids=("b",)),
            _step(step_id="b", optional_depends_on_step_ids=("a",)),
        ),
    )
    res = PlanValidator.validate(p, _ctx(), InMemoryCorpusRegistry())
    assert PlanViolationCode.CYCLE_DETECTED in {v.code for v in res.violations}


def test_optional_unknown_dependency_rejected() -> None:
    p = QueryPlan(
        plan_id="p",
        task_type="t",
        max_iterations=1,
        max_tool_calls=2,
        steps=(_step(step_id="a", optional_depends_on_step_ids=("ghost",)),),
    )
    res = PlanValidator.validate(p, _ctx(), InMemoryCorpusRegistry())
    assert PlanViolationCode.UNKNOWN_DEPENDENCY in {v.code for v in res.violations}


def test_diamond_dag_accepted() -> None:
    # a -> (b,c) -> d : a valid DAG with two parallel-ready middle steps.
    p = QueryPlan(
        plan_id="p",
        task_type="t",
        max_iterations=1,
        max_tool_calls=8,
        steps=(
            _step(step_id="a"),
            _step(step_id="b", depends_on_step_ids=("a",)),
            _step(step_id="c", depends_on_step_ids=("a",)),
            _step(step_id="d", depends_on_step_ids=("b", "c")),
        ),
    )
    res = PlanValidator.validate(p, _ctx(), InMemoryCorpusRegistry())
    assert PlanViolationCode.CYCLE_DETECTED not in {v.code for v in res.violations}
    assert res.accepted is True


# ---------------------------------------------------------------------------
# 3. Binding semantics: only declared-dependency outputs; template cannot bypass
# ---------------------------------------------------------------------------


def test_binding_to_undeclared_step_rejected() -> None:
    p = QueryPlan(
        plan_id="p",
        task_type="t",
        max_iterations=1,
        max_tool_calls=2,
        steps=(
            _step(step_id="a"),
            _step(step_id="b", input_bindings={"x": "steps.a.outputs.y"}),
        ),
    )
    res = PlanValidator.validate(p, _ctx(), InMemoryCorpusRegistry())
    assert PlanViolationCode.INVALID_BINDING in {v.code for v in res.violations}


def test_binding_to_optional_but_not_hard_dep_is_allowed() -> None:
    # Binding may reference an optional dependency (the executor decides continue-on-fail).
    p = QueryPlan(
        plan_id="p",
        task_type="t",
        max_iterations=1,
        max_tool_calls=2,
        steps=(
            _step(step_id="a"),
            _step(
                step_id="b",
                optional_depends_on_step_ids=("a",),
                input_bindings={"x": "steps.a.outputs.y"},
            ),
        ),
    )
    res = PlanValidator.validate(p, _ctx(), InMemoryCorpusRegistry())
    assert PlanViolationCode.INVALID_BINDING not in {v.code for v in res.violations}


def test_template_placeholder_without_declared_dep_rejected() -> None:
    p = QueryPlan(
        plan_id="p",
        task_type="t",
        max_iterations=1,
        max_tool_calls=2,
        steps=(_step(step_id="b", query_template="x {{a.y}} z"),),
    )
    res = PlanValidator.validate(p, _ctx(), InMemoryCorpusRegistry())
    assert PlanViolationCode.INVALID_BINDING in {v.code for v in res.violations}


def test_template_placeholder_must_reference_declared_step() -> None:
    p = QueryPlan(
        plan_id="p",
        task_type="t",
        max_iterations=1,
        max_tool_calls=2,
        steps=(
            _step(step_id="a"),
            _step(step_id="b", depends_on_step_ids=("a",), query_template="x {{a.y}} z"),
        ),
    )
    res = PlanValidator.validate(p, _ctx(), InMemoryCorpusRegistry())
    assert PlanViolationCode.INVALID_BINDING not in {v.code for v in res.violations}


# ---------------------------------------------------------------------------
# 4. Permission redaction: corpus name absent from message, serialized output, and logs
# ---------------------------------------------------------------------------


def test_unauthorized_corpus_absent_from_serialized_json() -> None:
    p = QueryPlan(
        plan_id="p",
        task_type="t",
        max_iterations=1,
        max_tool_calls=2,
        steps=(_step(step_id="a", target_corpus_ids=("top_secret",)),),
    )
    res = PlanValidator.validate(p, _ctx(), InMemoryCorpusRegistry())
    blob = res.model_dump_json()
    assert "top_secret" not in blob
    assert res.policy_violation_attempt is True


def test_unauthorized_corpus_absent_from_log_records() -> None:
    # The validator never surfaces the corpus name to a caller or log line. The violation
    # message is user-safe; the only place the name lives is `detail` (Field(exclude=True)),
    # which is excluded from both model_dump_json() and the default str() repr.
    p = QueryPlan(
        plan_id="p",
        task_type="t",
        max_iterations=1,
        max_tool_calls=2,
        steps=(_step(step_id="a", target_corpus_ids=("top_secret",)),),
    )
    res = PlanValidator.validate(p, _ctx(), InMemoryCorpusRegistry())
    safe_text = res.violations[0].message
    assert "top_secret" not in safe_text
    # str() of a frozen pydantic model omits excluded fields, so the name stays hidden.
    assert "top_secret" not in str(res.violations[0])
    assert "top_secret" not in str(res)
    # and JSON serialization (what an API would return) redacts it too.
    assert "top_secret" not in res.model_dump_json()


def test_unauthorized_corpus_error_not_raised_to_user() -> None:
    # The validator converts CorpusNotDiscoverableError internally; it never propagates.
    p = QueryPlan(
        plan_id="p",
        task_type="t",
        max_iterations=1,
        max_tool_calls=2,
        steps=(_step(step_id="a", target_corpus_ids=("top_secret",)),),
    )
    try:
        res = PlanValidator.validate(p, _ctx(), InMemoryCorpusRegistry())
    except Exception as exc:  # pragma: no cover
        pytest.fail(f"validator raised instead of returning a result: {exc}")
    assert res.accepted is False


# ---------------------------------------------------------------------------
# 5. Registry as single source of truth for authorization
# ---------------------------------------------------------------------------


class _CustomRegistry(CorpusRegistry):
    """A registry that does NOT consult any static corpus map — it only knows what is
    injected, proving the validator trusts the registry's accept/reject decision and
    never reads the returned corpus object (so a trivial stub is sufficient)."""

    def __init__(self, visible: set[str]) -> None:
        self._visible = visible

    def get(self, corpus_id: str, security_context: SecurityContext):
        from types import SimpleNamespace

        from agentic_rag_enterprise.retrieval.models import CorpusNotDiscoverableError

        if corpus_id in self._visible:
            # The validator only needs `registry.get` to NOT raise; it never inspects
            # the returned object, so a duck-typed stub proves there is no static-map
            # fallback.
            return SimpleNamespace(corpus_id=corpus_id)
        raise CorpusNotDiscoverableError(corpus_id)

    def list_searchable(self, security_context: SecurityContext):
        return []

    def resolve_candidates(self, query: str, security_context: SecurityContext, limit: int):
        return []


def test_validator_uses_registry_not_static_map() -> None:
    # 'engineering_wiki' exists in the static fixtures, but the custom registry does not
    # expose it -> validator must reject (proving it does not fall back to a static map).
    reg = _CustomRegistry(visible={"some_other_corpus"})
    p = QueryPlan(
        plan_id="p",
        task_type="t",
        max_iterations=1,
        max_tool_calls=2,
        steps=(_step(step_id="a", target_corpus_ids=("engineering_wiki",)),),
    )
    res = PlanValidator.validate(p, _ctx(), reg)
    assert PlanViolationCode.CORPUS_NOT_AUTHORIZED in {v.code for v in res.violations}
    assert res.accepted is False

    # The custom registry's corpus is accepted without touching the static fixtures.
    p2 = QueryPlan(
        plan_id="p",
        task_type="t",
        max_iterations=1,
        max_tool_calls=2,
        steps=(_step(step_id="a", target_corpus_ids=("some_other_corpus",)),),
    )
    res2 = PlanValidator.validate(p2, _ctx(), reg)
    assert res2.accepted is True


# ---------------------------------------------------------------------------
# 6. Capability + read-only boundary: unknown, sql/api write, spoofed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("cap", ["sql", "api", "graph"])
def test_reserved_write_capabilities_rejected(cap: str) -> None:
    p = QueryPlan(
        plan_id="p",
        task_type="t",
        max_iterations=1,
        max_tool_calls=2,
        steps=(_step(step_id="a", capability_id=cap),),
    )
    res = PlanValidator.validate(p, _ctx(), InMemoryCorpusRegistry())
    assert PlanViolationCode.CAPABILITY_NOT_ALLOWED in {v.code for v in res.violations}
    assert PlanViolationCode.WRITE_OPERATION in {v.code for v in res.violations}


def test_unknown_capability_rejected() -> None:
    p = QueryPlan(
        plan_id="p",
        task_type="t",
        max_iterations=1,
        max_tool_calls=2,
        steps=(_step(step_id="a", capability_id="lucene_fuzzy"),),
    )
    res = PlanValidator.validate(p, _ctx(), InMemoryCorpusRegistry())
    assert PlanViolationCode.CAPABILITY_NOT_ALLOWED in {v.code for v in res.violations}


@pytest.mark.parametrize("spoof", ["vector_search ", "VECTOR_SEARCH", "vector_search;drop"])
def test_capability_spoofing_fail_closed(spoof: str) -> None:
    # CapabilityCatalog.supports is exact-match; whitespace/case/sql-injection-like
    # strings are not 'supported'.
    assert CapabilityCatalog.supports(spoof) is False
    p = QueryPlan(
        plan_id="p",
        task_type="t",
        max_iterations=1,
        max_tool_calls=2,
        steps=(_step(step_id="a", capability_id=spoof),),
    )
    res = PlanValidator.validate(p, _ctx(), InMemoryCorpusRegistry())
    assert PlanViolationCode.CAPABILITY_NOT_ALLOWED in {v.code for v in res.violations}


# ---------------------------------------------------------------------------
# 7. Budget math: single, total, zero, negative, duplicate steps, optional branch
# ---------------------------------------------------------------------------


def test_zero_global_budget_accepts_zero_step() -> None:
    p = QueryPlan(
        plan_id="p",
        task_type="t",
        max_iterations=1,
        max_tool_calls=0,
        steps=(_step(step_id="a", max_tool_calls=0),),
    )
    res = PlanValidator.validate(p, _ctx(), InMemoryCorpusRegistry())
    # step budget (0) <= global (0) OK, total (0) <= 0 OK -> accepted
    assert res.accepted is True


def test_step_over_global_triggers_step_budget_violation() -> None:
    p = QueryPlan(
        plan_id="p",
        task_type="t",
        max_iterations=1,
        max_tool_calls=2,
        steps=(_step(step_id="a", max_tool_calls=5),),
    )
    res = PlanValidator.validate(p, _ctx(), InMemoryCorpusRegistry())
    assert PlanViolationCode.STEP_BUDGET_EXCEEDS_GLOBAL in {v.code for v in res.violations}


def test_budget_with_duplicate_steps_counts_each() -> None:
    # Two distinct steps, each bids 2, global 3 -> total 4 > 3 (each step <= global).
    p = QueryPlan(
        plan_id="p",
        task_type="t",
        max_iterations=1,
        max_tool_calls=3,
        steps=(_step(step_id="a", max_tool_calls=2), _step(step_id="b", max_tool_calls=2)),
    )
    res = PlanValidator.validate(p, _ctx(), InMemoryCorpusRegistry())
    assert PlanViolationCode.TOTAL_BUDGET_EXCEEDS_GLOBAL in {v.code for v in res.violations}
    assert PlanViolationCode.STEP_BUDGET_EXCEEDS_GLOBAL not in {v.code for v in res.violations}


def test_optional_branch_budget_still_counted() -> None:
    # Optional dependency does not change budget accounting: both steps counted.
    p = QueryPlan(
        plan_id="p",
        task_type="t",
        max_iterations=1,
        max_tool_calls=3,
        steps=(
            _step(step_id="a", max_tool_calls=2),
            _step(step_id="b", optional_depends_on_step_ids=("a",), max_tool_calls=2),
        ),
    )
    res = PlanValidator.validate(p, _ctx(), InMemoryCorpusRegistry())
    assert PlanViolationCode.TOTAL_BUDGET_EXCEEDS_GLOBAL in {v.code for v in res.violations}


# ---------------------------------------------------------------------------
# 8. Repair limit: at most one repair; callback exception + second invalid fail clearly
# ---------------------------------------------------------------------------


def test_repair_invoked_exactly_once_on_first_failure() -> None:
    calls = []
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

    def repair(d: dict) -> dict:
        calls.append(1)
        d["steps"][0]["step_id"] = "s1"
        return d

    parse_plan(bad, repair_fn=repair)
    assert len(calls) == 1


def test_repair_callback_exception_propagates() -> None:
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

    def repair(d: dict) -> dict:
        raise ValueError("repair impl exploded")

    with pytest.raises(ValueError):
        parse_plan(bad, repair_fn=repair)


def test_repair_second_invalid_raises_exhausted() -> None:
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

    def repair(d: dict) -> dict:
        return d  # no fix -> still missing step_id

    with pytest.raises(PlanRepairExhaustedError):
        parse_plan(bad, repair_fn=repair)


def test_repair_not_invoked_when_parse_succeeds() -> None:
    calls = []
    good = {
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
    parse_plan(good, repair_fn=lambda d: calls.append(1) or d)
    assert calls == []


def test_repair_accepts_json_string_raw() -> None:
    calls = []
    bad_json = json.dumps(
        {
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
    )

    def repair(d: dict) -> dict:
        calls.append(1)
        d["steps"][0]["step_id"] = "s1"
        return d

    plan = parse_plan(bad_json, repair_fn=repair)
    assert isinstance(plan, QueryPlan)
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# 9. Pure control-plane boundary: no retriever/executor/service/agent imports
# ---------------------------------------------------------------------------


def test_planner_imports_no_execution_surface() -> None:
    import ast
    import pathlib

    planner_dir = (
        pathlib.Path(__file__).resolve().parents[1] / "src" / "agentic_rag_enterprise" / "planner"
    )
    forbidden = {
        "agentic_rag_enterprise.retrieval.retriever",
        "agentic_rag_enterprise.retrieval.secure_retriever",
        "agentic_rag_enterprise.retrieval.fast_path",
        "agentic_rag_enterprise.services.chat_service",
        "agentic_rag_enterprise.agents",
        "agentic_rag_enterprise.graph",
    }
    found = set()
    for path in planner_dir.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                if node.module in forbidden or node.module.startswith(
                    "agentic_rag_enterprise.retrieval.retriever"
                ):
                    found.add(node.module)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in forbidden:
                        found.add(alias.name)
    assert found == set(), f"forbidden imports: {found}"


# ---------------------------------------------------------------------------
# 10. E-018 readiness: parallel-ready steps, dep output binding, optional dep, step budget
# ---------------------------------------------------------------------------


def test_contract_expresses_parallel_ready_and_optional_and_binding() -> None:
    """The frozen contract can represent everything E-018 needs without execution code:
    - a (root) is parallel-ready (no deps)
    - b and c are parallel-ready siblings depending on a
    - d depends on b (hard) and on c (optional) and binds b's output
    - each step carries its own max_tool_calls for the atomic budget allocator
    """
    p = QueryPlan(
        plan_id="p",
        task_type="dependent_multi_hop",
        max_iterations=1,
        max_tool_calls=8,
        required_facts=(
            RequiredFact(fact_id="F1", description="x"),
            RequiredFact(fact_id="F2", description="y"),
        ),
        steps=(
            _step(
                step_id="a",
                target_corpus_ids=("engineering_wiki",),
                query="find entity",
                output_schema_id="entity",
                max_tool_calls=2,
            ),
            _step(
                step_id="b",
                depends_on_step_ids=("a",),
                target_corpus_ids=("product_docs",),
                query="b query",
                output_schema_id="spec",
                max_tool_calls=2,
            ),
            _step(
                step_id="c",
                depends_on_step_ids=("a",),
                target_corpus_ids=("product_docs",),
                query="c query",
                output_schema_id="spec",
                max_tool_calls=2,
            ),
            _step(
                step_id="d",
                depends_on_step_ids=("b",),
                optional_depends_on_step_ids=("c",),
                target_corpus_ids=("product_docs",),
                query_template="synthesize {{b.out}} with {{c.out}}",
                input_bindings={"b_out": "steps.b.outputs.out", "c_out": "steps.c.outputs.out"},
                output_schema_id="intermediate",
                max_tool_calls=2,
            ),
        ),
    )
    res = PlanValidator.validate(p, _ctx(), InMemoryCorpusRegistry())
    assert res.accepted is True
    # structural facts E-018 will rely on:
    by_id = {s.step_id: s for s in p.steps}
    assert by_id["a"].depends_on_step_ids == ()
    assert by_id["b"].depends_on_step_ids == ("a",)
    assert by_id["c"].depends_on_step_ids == ("a",)
    assert set(by_id["d"].depends_on_step_ids) == {"b"}
    assert by_id["d"].optional_depends_on_step_ids == ("c",)
    assert by_id["d"].input_bindings == {
        "b_out": "steps.b.outputs.out",
        "c_out": "steps.c.outputs.out",
    }
    assert sum(s.max_tool_calls for s in p.steps) == 8 == p.max_tool_calls
