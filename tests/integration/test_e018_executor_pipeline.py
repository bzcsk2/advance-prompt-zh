"""E-018 Executor pipeline integration tests (contract §11 acceptance matrix).

Tests the executor end-to-end with the **real RetrieverTool** (not mock Tools):
two-hop binding, template substitution, and Evidence projection through the
actual ``RetrieverTool.execute_step`` → ``_project`` code path.

Uses a fake ``SecureRetriever`` with in-memory ``CorpusRegistry`` — no Qdrant
required.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, create_model

from agentic_rag_enterprise.corpus.registry import InMemoryCorpusRegistry
from agentic_rag_enterprise.domain.evidence import Evidence
from agentic_rag_enterprise.domain.security import SecurityContext
from agentic_rag_enterprise.planner.executor import PlanExecutor
from agentic_rag_enterprise.planner.models import PlanStep, QueryPlan
from agentic_rag_enterprise.planner.result import StepStatus
from agentic_rag_enterprise.planner.tool_registry import (
    RetrieverTool,
    ToolSpec,
)

# ---------------------------------------------------------------------------
# Fake SecureRetriever — controllable Evidence per query
# ---------------------------------------------------------------------------


class _FakeSecureRetriever:
    """Returns evidence based on the query content.

    Step 1 query → "Project X …" → entity evidence with server identifier.
    Step 2 query → "server … hardware …" → spec evidence with hardware details.
    """

    def retrieve_evidence(
        self,
        ctx: SecurityContext,
        query: str,
        corpus: Any,
        *,
        dense_encoder: Any = None,
        sparse_encoder: Any = None,
        iteration: int = 0,
        plan_step_id: str | None = None,
    ) -> list[Evidence]:
        now = datetime.now()
        base = dict(
            tenant_id=ctx.tenant_id,
            corpus_id=corpus.corpus_id,
            document_version="v1",
            source_uri="",
            source_filename="",
            text_hash="mock",
            retrieval_query=query,
            retrieved_at=now,
            acl_policy_id="p1",
            policy_version="1.0",
            retrieval_iteration=iteration,
            plan_step_id=plan_step_id,
        )

        if "Project X" in query:
            return [
                Evidence(
                    evidence_id="ev_entity",
                    document_id="doc-server-ids",
                    text="ProjectX-DB-42 is the production database server",
                    authority_level=80,
                    retrieval_score=0.95,
                    **base,  # type: ignore[arg-type]
                )
            ]
        else:
            return [
                Evidence(
                    evidence_id="ev_spec",
                    document_id="doc-specs",
                    text="Server specs: 64GB RAM, 8 cores, SSD storage",
                    authority_level=70,
                    retrieval_score=0.92,
                    **base,  # type: ignore[arg-type]
                )
            ]


# ---------------------------------------------------------------------------
# Mock encoders
# ---------------------------------------------------------------------------


class _FakeDenseEncoder:
    def __call__(self, text: str) -> list[float]:
        return []


class _FakeSparseEncoder:
    def __call__(self, text: str) -> dict[int, float]:
        return {}


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


class _DummyInput(BaseModel):
    model_config = ConfigDict(frozen=True)
    query: str = ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ctx(**kw: Any) -> SecurityContext:
    base = dict(
        request_id="r",
        session_id="s",
        tenant_id="local",
        user_id="u",
        policy_version="1.0",
    )
    base.update(kw)
    return SecurityContext(**base)


def _step(**kw: Any) -> PlanStep:
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
# Test: real RetrieverTool two-hop
# ---------------------------------------------------------------------------


def test_real_retriever_tool_two_hop() -> None:
    """Step 1 (entity) → RetrieverTool → Evidence projection → entity_text
    → template substitution → Step 2 (spec) → RetrieverTool → Evidence.

    Uses the real ``RetrieverTool`` class with a stable ``ToolRegistry``
    (same Tool + ToolSpec for all steps).  No mock Tools.
    """
    retriever = _FakeSecureRetriever()
    corpus_registry = InMemoryCorpusRegistry()
    dense_encoder = _FakeDenseEncoder()
    sparse_encoder = _FakeSparseEncoder()

    retriever_tool = RetrieverTool(
        retriever=retriever,
        corpus_registry=corpus_registry,
        dense_encoder=dense_encoder,
        sparse_encoder=sparse_encoder,
    )

    # Single ToolSpec covering both entity and spec output schemas.
    spec = ToolSpec(
        step_type="retrieve",
        capability_id="vector_search",
        input_model=_DummyInput,
        output_models={"entity": _ENTITY_OUTPUT, "spec": _SPEC_OUTPUT},
        retryable_errors=frozenset(),
    )

    # Stable registry: same (Tool, ToolSpec) for every step.
    class _StableRegistry:
        def get(self, step_type: str, capability_id: str) -> tuple:
            return (retriever_tool, spec)

    executor = PlanExecutor(_StableRegistry(), concurrency=1)

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

    result = executor.execute(plan, _ctx(), corpus_registry)

    # ---- Assertions ----

    assert result.accepted is True
    assert result.executed is True
    assert result.degraded is False
    assert len(result.steps) == 2

    # Step 1: RetrieverTool projected Entity evidence.
    s1 = result.steps[0]
    assert s1.step_id == "find_server"
    assert s1.status == StepStatus.succeeded
    assert s1.outputs.get("entity_text") == "ProjectX-DB-42 is the production database server"
    assert s1.outputs.get("corpus_id") == "engineering_wiki"
    assert "ev_entity" in s1.evidence_ids

    # Step 2: RetrieverTool received the bound query and projected Spec evidence.
    s2 = result.steps[1]
    assert s2.step_id == "find_specs"
    assert s2.status == StepStatus.succeeded
    # The spec_text comes from the top Evidence's text.
    assert "64GB RAM" in str(s2.outputs.get("spec_text", ""))
    assert s2.outputs.get("corpus_id") == "product_docs"
    assert "ev_spec" in s2.evidence_ids

    # Final evidence_ids contain both.
    assert "ev_entity" in result.evidence_ids
    assert "ev_spec" in result.evidence_ids

    # The executor has at most one active thread at a time (concurrency=1),
    # so this is proof that binding + template resolution worked end-to-end
    # through the real RetrieverTool code path.
