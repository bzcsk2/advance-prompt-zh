"""E-017 Planner DAG Validator (build plan §13.3 / §13.5).

Static, pre-execution validation of a :class:`QueryPlan`. The validator performs **no**
retrieval: it only consults the fail-closed ``registry.get(corpus_id, ctx)`` for corpus
authorization and ``CapabilityCatalog.supports`` for capability allowlisting. This is
what makes the M5 exit-gate guarantee "illegal DAG runs zero Tools" structurally true —
there is no execution surface in E-017 at all.

The validator is *collect-all*: it always runs every check and reports every defect in a
single :class:`PlanValidationResult`, never early-returning on the first failure.
"""

from __future__ import annotations

import re

from agentic_rag_enterprise.corpus.capability_registry import CapabilityCatalog
from agentic_rag_enterprise.corpus.registry import CorpusRegistry
from agentic_rag_enterprise.domain.security import SecurityContext
from agentic_rag_enterprise.planner.binding import (
    BindingExpression,
    binding_violation,
)
from agentic_rag_enterprise.planner.models import (
    PlanValidationResult,
    PlanViolation,
    PlanViolationCode,
    QueryPlan,
    StepDependency,
)
from agentic_rag_enterprise.retrieval.models import CorpusNotDiscoverableError

# Write capabilities are reserved-but-disabled in M5 (build plan §9.1 / §13.5).
_WRITE_CAPABILITIES = frozenset({"sql", "api", "graph"})
_TMPL_FIND_RE = re.compile(r"\{\{[^}]+\}\}")


class PlanValidator:
    """Static pre-execution validator for :class:`QueryPlan` (build plan §13.3)."""

    @staticmethod
    def validate(
        plan: QueryPlan,
        ctx: SecurityContext,
        registry: CorpusRegistry,
    ) -> PlanValidationResult:
        """Validate ``plan`` against ``ctx`` authorization and the static contract.

        Never raises on an invalid plan — returns ``accepted=False`` with violations.
        ``CorpusNotDiscoverableError`` from the registry is caught and converted into a
        user-safe ``CORPUS_NOT_AUTHORIZED`` violation (the corpus name stays in
        ``detail`` only, ``exclude=True``); it is never propagated.
        """
        violations: list[PlanViolation] = []
        policy_violation_attempt = False

        step_ids = plan.step_ids()
        unique_step_ids = set(step_ids)
        fact_ids = set(plan.required_fact_ids())

        # 1. step_id uniqueness
        seen: dict[str, None] = {}
        for sid in step_ids:
            if sid in seen:
                violations.append(
                    PlanViolation.user_safe(
                        PlanViolationCode.DUPLICATE_STEP_ID,
                        "plan contains duplicate step identifiers",
                        detail=sid,
                        step_id=sid,
                    )
                )
            seen.setdefault(sid, None)

        # 2. dependency existence + duplicates + self-cycle
        for step in plan.steps:
            for dep in step.depends_on_step_ids:
                if dep == step.step_id:
                    violations.append(
                        PlanViolation.user_safe(
                            PlanViolationCode.CYCLE_DETECTED,
                            "plan contains a cyclic dependency",
                            detail=f"{step.step_id} depends on itself",
                            step_id=step.step_id,
                        )
                    )
                elif dep not in unique_step_ids:
                    violations.append(
                        PlanViolation.user_safe(
                            PlanViolationCode.UNKNOWN_DEPENDENCY,
                            "plan references a step dependency that does not exist",
                            detail=dep,
                            step_id=step.step_id,
                        )
                    )
            for dep in step.optional_depends_on_step_ids:
                if dep == step.step_id:
                    violations.append(
                        PlanViolation.user_safe(
                            PlanViolationCode.CYCLE_DETECTED,
                            "plan contains a cyclic dependency",
                            detail=f"{step.step_id} optionally depends on itself",
                            step_id=step.step_id,
                        )
                    )
                elif dep not in unique_step_ids:
                    violations.append(
                        PlanViolation.user_safe(
                            PlanViolationCode.UNKNOWN_DEPENDENCY,
                            "plan references a step dependency that does not exist",
                            detail=dep,
                            step_id=step.step_id,
                        )
                    )
            overlap = set(step.depends_on_step_ids) & set(step.optional_depends_on_step_ids)
            for dep in overlap:
                violations.append(
                    PlanViolation.user_safe(
                        PlanViolationCode.DUPLICATE_DEPENDENCY,
                        "plan lists the same step as both a hard and optional dependency",
                        detail=dep,
                        step_id=step.step_id,
                    )
                )

        # 3. DAG cycle detection (Kahn's algorithm over hard + optional edges)
        if not any(v.code == PlanViolationCode.DUPLICATE_STEP_ID for v in violations):
            edges = PlanValidator._build_edges(plan)
            if PlanValidator._has_cycle(edges, unique_step_ids):
                violations.append(
                    PlanViolation.user_safe(
                        PlanViolationCode.CYCLE_DETECTED,
                        "plan is not a directed acyclic graph",
                    )
                )

        # 4. corpus authorization (fail-closed via registry.get)
        for step in plan.steps:
            for corpus_id in step.target_corpus_ids:
                try:
                    registry.get(corpus_id, ctx)
                except CorpusNotDiscoverableError:
                    policy_violation_attempt = True
                    violations.append(
                        PlanViolation.user_safe(
                            PlanViolationCode.CORPUS_NOT_AUTHORIZED,
                            "plan targets a corpus that is not authorized for this request",
                            detail=corpus_id,
                            step_id=step.step_id,
                        )
                    )

        # 5. capability allowlist
        for step in plan.steps:
            if not CapabilityCatalog.supports(step.capability_id):
                violations.append(
                    PlanViolation.user_safe(
                        PlanViolationCode.CAPABILITY_NOT_ALLOWED,
                        "plan requests a capability that is not allowed for this request",
                        detail=step.capability_id,
                        step_id=step.step_id,
                    )
                )

        # 6. static budget pre-validation (confirmed scope)
        for step in plan.steps:
            if step.max_tool_calls > plan.max_tool_calls:
                violations.append(
                    PlanViolation.user_safe(
                        PlanViolationCode.STEP_BUDGET_EXCEEDS_GLOBAL,
                        "plan contains a step whose budget exceeds the global budget",
                        detail=f"{step.step_id}: {step.max_tool_calls} > {plan.max_tool_calls}",
                        step_id=step.step_id,
                    )
                )
        total = sum(s.max_tool_calls for s in plan.steps)
        if total > plan.max_tool_calls:
            violations.append(
                PlanViolation.user_safe(
                    PlanViolationCode.TOTAL_BUDGET_EXCEEDS_GLOBAL,
                    "plan total budget exceeds the global budget",
                    detail=f"sum={total} > {plan.max_tool_calls}",
                )
            )

        # 7. query non-empty
        for step in plan.steps:
            if not step.query and not step.query_template:
                violations.append(
                    PlanViolation.user_safe(
                        PlanViolationCode.EMPTY_QUERY,
                        "plan contains a step with no query or query template",
                        step_id=step.step_id,
                    )
                )

        # 8. input_bindings well-formedness + template placeholders
        for step in plan.steps:
            declared_deps = set(step.all_dependency_ids())
            for field_name, raw in step.input_bindings.items():
                try:
                    expr = BindingExpression.parse(raw)
                except Exception:
                    violations.append(binding_violation(raw, step.step_id))
                    continue
                if expr.kind.value == "step_output":
                    if expr.step_id not in declared_deps:
                        violations.append(
                            PlanViolation.user_safe(
                                PlanViolationCode.INVALID_BINDING,
                                "plan binds to a step that is not a declared dependency",
                                detail=f"{field_name} -> {raw}",
                                step_id=step.step_id,
                            )
                        )
                    elif not (expr.output_field or "").strip():
                        violations.append(
                            PlanViolation.user_safe(
                                PlanViolationCode.INVALID_BINDING,
                                "plan binds to an output field that is not an identifier",
                                detail=f"{field_name} -> {raw}",
                                step_id=step.step_id,
                            )
                        )
                elif expr.kind.value == "fact_value":
                    if expr.fact_id not in fact_ids:
                        violations.append(
                            PlanViolation.user_safe(
                                PlanViolationCode.INVALID_BINDING,
                                "plan binds to a required fact that does not exist",
                                detail=f"{field_name} -> {raw}",
                                step_id=step.step_id,
                            )
                        )
            if step.query_template:
                for ph in _TMPL_FIND_RE.findall(step.query_template):
                    try:
                        expr = BindingExpression.parse_template_placeholder(ph)
                    except Exception:
                        violations.append(
                            PlanViolation.user_safe(
                                PlanViolationCode.INVALID_BINDING,
                                "plan query template contains an invalid placeholder",
                                detail=ph,
                                step_id=step.step_id,
                            )
                        )
                        continue
                    if expr.step_id not in declared_deps:
                        violations.append(
                            PlanViolation.user_safe(
                                PlanViolationCode.INVALID_BINDING,
                                "plan query template references a step that is not a declared dependency",
                                detail=ph,
                                step_id=step.step_id,
                            )
                        )

        # 9. no write operation (defensive: Literals already exclude unknown types/schemas)
        for step in plan.steps:
            if step.capability_id in _WRITE_CAPABILITIES:
                violations.append(
                    PlanViolation.user_safe(
                        PlanViolationCode.WRITE_OPERATION,
                        "plan requests a write capability which is not permitted",
                        detail=step.capability_id,
                        step_id=step.step_id,
                    )
                )

        return PlanValidationResult(
            accepted=len(violations) == 0,
            violations=tuple(violations),
            policy_violation_attempt=policy_violation_attempt,
        )

    @staticmethod
    def _build_edges(plan: QueryPlan) -> list[StepDependency]:
        edges: list[StepDependency] = []
        by_id = {s.step_id: s for s in plan.steps}
        for step in plan.steps:
            for dep in step.depends_on_step_ids:
                if dep in by_id:
                    edges.append(
                        StepDependency(
                            upstream_step_id=dep,
                            downstream_step_id=step.step_id,
                            optional=False,
                        )
                    )
            for dep in step.optional_depends_on_step_ids:
                if dep in by_id:
                    edges.append(
                        StepDependency(
                            upstream_step_id=dep,
                            downstream_step_id=step.step_id,
                            optional=True,
                        )
                    )
        return edges

    @staticmethod
    def _has_cycle(edges: list[StepDependency], step_ids: set[str]) -> bool:
        """Kahn's algorithm: True iff a cycle exists among the edges."""
        indeg = {sid: 0 for sid in step_ids}
        adj: dict[str, list[str]] = {sid: [] for sid in step_ids}
        for e in edges:
            if e.upstream_step_id in indeg and e.downstream_step_id in indeg:
                indeg[e.downstream_step_id] += 1
                adj[e.upstream_step_id].append(e.downstream_step_id)
        queue = [sid for sid in step_ids if indeg[sid] == 0]
        visited = 0
        while queue:
            u = queue.pop()
            visited += 1
            for v in adj[u]:
                indeg[v] -= 1
                if indeg[v] == 0:
                    queue.append(v)
        return visited != len(step_ids)
