"""E-018 Executor pipeline integration tests (contract §11 acceptance matrix).

Tests the executor end-to-end with realistic Tool behaviour: two-hop binding,
template substitution, and output collection.
"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, create_model

from agentic_rag_enterprise.corpus.registry import InMemoryCorpusRegistry
from agentic_rag_enterprise.domain.security import SecurityContext
from agentic_rag_enterprise.planner.executor import PlanExecutor
from agentic_rag_enterprise.planner.models import PlanStep, QueryPlan
from agentic_rag_enterprise.planner.result import StepStatus
from agentic_rag_enterprise.planner.tool_registry import (
    Tool,
    ToolSpec,
    TypedStepOutput,
    _RESOLVED_QUERY_KEY,
)

# ---------------------------------------------------------------------------
# Output models matching the contract §10a projection
# ---------------------------------------------------------------------------

_ENTITY_OUTPUT = create_model(
    "EntityOutput",
    entity_text=(str, ...),
    corpus_id=(str, ...),
    document_id=(str, ""),
    section_path=(tuple[str, ...], ()),
    authority_level=(int, 0),
)

_SPEC_OUTPUT = create_model(
    "SpecOutput",
    spec_text=(str, ...),
    corpus_id=(str, ...),
    document_id=(str, ""),
    metadata=(dict[str, object], {}),
)


def _ctx(**kw: dict) -> SecurityContext:
    base = dict(
        request_id="r",
        session_id="s",
        tenant_id="local",
        user_id="u",
        policy_version="1.0",
    )
    base.update(kw)
    return SecurityContext(**base)


def _step(**kw: dict) -> PlanStep:
    base = dict(
        step_id="s1",
        step_type="retrieve",
        description="d",
        target_corpus_ids=("engineering_wiki",),
        capability_id="vector_search",
        query="test query",
        output_schema_id="entity",
        max_tool_calls=2,
    )
    base.update(kw)
    return PlanStep(**base)


# ---------------------------------------------------------------------------
# Two-hop binding test
# ---------------------------------------------------------------------------


def test_two_hop_binding_happy_path() -> None:
    """Step 1 (entity) output is bound into Step 2 query template.

    This proves the executor correctly:
    1. Resolves Step 1's query and runs it.
    2. Collects Step 1's entity_text output.
    3. Substitutes the entity_text into Step 2's query_template.
    4. Passes the resolved query to Step 2's Tool.
    """
    step2_captured: list[str] = []

    class _EntityTool:
        """Step 1: returns an entity result."""

        def execute_step(
            self,
            step: PlanStep,
            resolved_inputs: Mapping[str, object],
            ctx: SecurityContext,
        ) -> TypedStepOutput:
            return TypedStepOutput(
                outputs={
                    "entity_text": "ProjectX-DB-42",
                    "corpus_id": "engineering_wiki",
                    "document_id": "doc-server-ids",
                    "authority_level": 80,
                },
                evidence_ids=("ev_entity",),
                schema_id=step.output_schema_id,
            )

    class _SpecTool:
        """Step 2: captures the resolved query, returns spec output."""

        def execute_step(
            self,
            step: PlanStep,
            resolved_inputs: Mapping[str, object],
            ctx: SecurityContext,
        ) -> TypedStepOutput:
            query = str(resolved_inputs.get(_RESOLVED_QUERY_KEY, ""))
            step2_captured.append(query)
            return TypedStepOutput(
                outputs={
                    "spec_text": f"Specs for server {query}",
                    "corpus_id": "product_docs",
                    "document_id": "doc-specs",
                    "metadata": {"authority_level": 70, "retrieval_score": 0.95},
                },
                evidence_ids=("ev_spec",),
                schema_id=step.output_schema_id,
            )

    # Build the ToolSpecs.
    class _DummyInput(BaseModel):
        model_config = ConfigDict(frozen=True)
        query: str = ""

    entity_spec = ToolSpec(
        step_type="retrieve",
        capability_id="vector_search",
        input_model=_DummyInput,
        output_models={"entity": _ENTITY_OUTPUT},
        retryable_errors=frozenset(),
    )
    spec_spec = ToolSpec(
        step_type="retrieve",
        capability_id="vector_search",
        input_model=_DummyInput,
        output_models={"spec": _SPEC_OUTPUT},
        retryable_errors=frozenset(),
    )

    # Registry that returns the right Tool+Spec for each step.
    class _TwoHopRegistry:
        def get(self, step_type: str, capability_id: str) -> tuple[Tool, ToolSpec]:
            return (_EntityTool(), entity_spec)

    class _Step2Registry:
        def get(self, step_type: str, capability_id: str) -> tuple[Tool, ToolSpec]:
            return (_SpecTool(), spec_spec)

    # Use a custom executor that switches registry per step.
    class _SwitchingRegistry:
        def __init__(self) -> None:
            self._call_count = 0

        def get(self, step_type: str, capability_id: str) -> tuple[Tool, ToolSpec]:
            self._call_count += 1
            if self._call_count == 1:
                return (_EntityTool(), entity_spec)
            return (_SpecTool(), spec_spec)

    executor = PlanExecutor(_SwitchingRegistry(), concurrency=1)

    plan = QueryPlan(
        plan_id="p1",
        task_type="dependent_multi_hop",
        max_iterations=1,
        max_tool_calls=10,
        steps=(
            _step(
                step_id="find_server",
                target_corpus_ids=("engineering_wiki",),
                query="Project X production server identifier",
                output_schema_id="entity",
            ),
            _step(
                step_id="find_specs",
                depends_on_step_ids=("find_server",),
                target_corpus_ids=("product_docs",),
                query=None,
                query_template="server {{find_server.entity_text}} hardware specifications",
                input_bindings={
                    "server_id": "steps.find_server.outputs.entity_text",
                },
                output_schema_id="spec",
            ),
        ),
    )

    result = executor.execute(plan, _ctx(), InMemoryCorpusRegistry())

    # ---- Assertions ----

    assert result.accepted is True
    assert result.executed is True
    assert result.degraded is False  # both steps succeeded
    assert len(result.steps) == 2

    # Step 1: find_server succeeded.
    assert result.steps[0].step_id == "find_server"
    assert result.steps[0].status == StepStatus.succeeded
    assert result.steps[0].outputs.get("entity_text") == "ProjectX-DB-42"

    # Step 2: find_specs succeeded.  The template {{find_server.entity_text}}
    # was replaced with "ProjectX-DB-42", producing the resolved query:
    # "server ProjectX-DB-42 hardware specifications"
    assert result.steps[1].step_id == "find_specs"
    assert result.steps[1].status == StepStatus.succeeded
    resolved_query = step2_captured[0]
    assert "ProjectX-DB-42" in resolved_query
    assert resolved_query.startswith("server ")
    assert resolved_query.endswith("hardware specifications")

    # The resolved query must contain the bound entity_text.
    assert len(step2_captured) == 1
    assert "ProjectX-DB-42" in step2_captured[0]

    # evidence_ids from both steps are collected.
    assert "ev_entity" in result.evidence_ids
    assert "ev_spec" in result.evidence_ids
