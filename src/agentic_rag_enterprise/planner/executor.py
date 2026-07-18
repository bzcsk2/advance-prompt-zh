"""E-018 PlanExecutor — Controlled DAG Executor (build plan §13.4, contract E-018).

Executes an accepted :class:`QueryPlan` against the registered Tools, respecting
the atomic budget, required/optional dependencies, timeout, retry, and failure
degradation rules frozen in the E-018 contract.
"""

from __future__ import annotations

import concurrent.futures
import re
from typing import Any

from pydantic import BaseModel

from agentic_rag_enterprise.answer.envelope import TenantBindingError
from agentic_rag_enterprise.corpus.registry import CorpusRegistry
from agentic_rag_enterprise.domain.security import SecurityContext
from agentic_rag_enterprise.planner.binding import BindingExpression, BindingKind
from agentic_rag_enterprise.planner.budget import AtomicToolBudget
from agentic_rag_enterprise.planner.errors import PlanExecutionError
from agentic_rag_enterprise.planner.models import (
    PlanStep,
    QueryPlan,
)
from agentic_rag_enterprise.planner.result import (
    PlanExecutionResult,
    StepResult,
    StepStatus,
)
from agentic_rag_enterprise.planner.tool_registry import (
    Tool,
    ToolRegistry,
    ToolSpec,
    _RESOLVED_QUERY_KEY,
)
from agentic_rag_enterprise.planner.validator import PlanValidator
from agentic_rag_enterprise.retrieval.models import (
    CorpusNotDiscoverableError,
    ParentAuthorizationError,
)
from agentic_rag_enterprise.security.filter import EmptyAuthorizationScopeError

# ---------------------------------------------------------------------------
# Template placeholder regex (reuses validator's pattern)
# ---------------------------------------------------------------------------
_TMPL_FIND_RE = re.compile(r"\{\{[^}]+\}\}")
_TMPL_PARSE_RE = re.compile(r"^\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)\s*\}\}$")

# Security binding exceptions — any of these from a Tool causes immediate
# whole-execution fail-closed (contract §9).
_SECURITY_BINDING_ERRORS = (
    CorpusNotDiscoverableError,
    ParentAuthorizationError,
    EmptyAuthorizationScopeError,
    TenantBindingError,
)

# Programming errors — never retried (contract §8).
_PROGRAMMING_ERRORS = (ValueError, TypeError, KeyError)


class PlanExecutor:
    """Executor for a validated :class:`QueryPlan`.

    ``tool_registry`` provides the Tool + ToolSpec lookup for each step.
    ``concurrency`` controls the maximum number of parallel step executions within
    a single topological layer.
    """

    def __init__(
        self,
        tool_registry: ToolRegistry,
        *,
        concurrency: int = 4,
    ) -> None:
        self._tool_registry = tool_registry
        self._concurrency = concurrency

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def execute(
        self,
        plan: QueryPlan,
        ctx: SecurityContext,
        corpus_registry: CorpusRegistry,
    ) -> PlanExecutionResult:
        """Execute the plan and return a result.

        Raises:
            PlanExecutionError: if the plan is rejected by pre-validation,
                a security binding failure occurs, or zero usable Evidence
                is produced.
        """
        # ---- 1. Re-validate ----
        validation = PlanValidator.validate(plan, ctx, corpus_registry)
        if not validation.accepted:
            raise PlanExecutionError(
                "plan failed pre-execution validation",
                error_code="plan_rejected",
                detail=f"violations: {[v.detail for v in validation.violations]}",
            )

        # ---- 2. Pre-check multi-corpus budget ----
        for step in plan.steps:
            n_corpora = len(step.target_corpus_ids)
            if step.max_tool_calls < n_corpora:
                raise PlanExecutionError(
                    "plan contains a step whose budget is less than its corpus count",
                    error_code="budget_insufficient_for_corpora",
                    detail=f"step={step.step_id} max_tool_calls={step.max_tool_calls} "
                    f"corpora={n_corpora}",
                )

        # ---- 3. Build dependencies and topological layers ----
        step_map: dict[str, PlanStep] = {s.step_id: s for s in plan.steps}
        layers = _build_topological_layers(plan)
        budget = AtomicToolBudget(plan.max_tool_calls)
        fact_values = {f.fact_id: f.description for f in plan.required_facts}

        # Maps step_id -> StepResult (populated as steps complete).
        results: dict[str, StepResult] = {}
        any_tool_launched = False
        limitations: list[str] = []

        # ---- 4. Execute layers ----
        for layer in layers:
            layer_results = self._execute_layer(
                layer, step_map, results, fact_values, budget, ctx, limitations
            )
            results.update(layer_results)
            if any(r.tool_calls_consumed > 0 for r in layer_results.values()):
                any_tool_launched = True

        # ---- 5. Build final result ----
        return self._build_result(
            plan,
            results,
            any_tool_launched,
            limitations,
            budget,
        )

    # ------------------------------------------------------------------
    # Layer execution
    # ------------------------------------------------------------------

    def _execute_layer(
        self,
        layer: list[str],
        step_map: dict[str, PlanStep],
        completed: dict[str, StepResult],
        fact_values: dict[str, str],
        budget: AtomicToolBudget,
        ctx: SecurityContext,
        limitations: list[str],
    ) -> dict[str, StepResult]:
        """Execute all ready steps in a topological layer (possibly in parallel)."""
        # Submit all steps in the layer to the thread pool.
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(self._concurrency, len(layer))
        ) as pool:
            future_to_step: dict[concurrent.futures.Future, str] = {}
            for step_id in layer:
                future = pool.submit(
                    self._execute_single_step,
                    step_id,
                    step_map,
                    completed,
                    fact_values,
                    budget,
                    ctx,
                )
                future_to_step[future] = step_id

            layer_results: dict[str, StepResult] = {}
            for future in concurrent.futures.as_completed(future_to_step):
                step_id = future_to_step[future]
                try:
                    result = future.result()
                except PlanExecutionError:
                    raise  # security binding failure — propagate immediately
                except Exception as exc:
                    # Unexpected error (programming bug) — terminal failed.
                    result = StepResult(
                        step_id=step_id,
                        status=StepStatus.failed,
                        error_code="unexpected_error",
                        message="an unexpected error occurred",
                        detail=f"{type(exc).__name__}: {exc}",
                    )
                layer_results[step_id] = result
                if result.status in (
                    StepStatus.failed,
                    StepStatus.timed_out,
                    StepStatus.skipped_dependency,
                    StepStatus.budget_exhausted,
                ):
                    limitations.append(f"step {step_id}: {result.status.value}")
            return layer_results

    # ------------------------------------------------------------------
    # Single-step execution (including retry)
    # ------------------------------------------------------------------

    def _execute_single_step(
        self,
        step_id: str,
        step_map: dict[str, PlanStep],
        completed: dict[str, StepResult],
        fact_values: dict[str, str],
        budget: AtomicToolBudget,
        ctx: SecurityContext,
    ) -> StepResult:
        step = step_map[step_id]
        tool, spec = self._tool_registry.get(step.step_type, step.capability_id)

        # ---- Check required dependencies ----
        for dep_id in step.depends_on_step_ids:
            dep_result = completed.get(dep_id)
            if dep_result is None or dep_result.status != StepStatus.succeeded:
                return StepResult(
                    step_id=step_id,
                    status=StepStatus.skipped_dependency,
                    message="a required upstream step did not succeed",
                    detail=f"upstream {dep_id} status="
                    f"{(dep_result.status.value if dep_result else 'not_started')}",
                )

        # ---- Resolve inputs and check optional bindings ----
        try:
            resolved_inputs = _resolve_inputs(step, completed, fact_values, spec)
        except PlanExecutionError:
            raise  # security binding failure
        except Exception as exc:
            return StepResult(
                step_id=step_id,
                status=StepStatus.failed,
                error_code="binding_error",
                message="binding resolution failed",
                detail=f"{type(exc).__name__}: {exc}",
            )

        # ---- Validate resolved inputs against ToolSpec.input_model ----
        missing_required = _check_missing_required(spec.input_model, resolved_inputs)
        if missing_required:
            return StepResult(
                step_id=step_id,
                status=StepStatus.failed,
                error_code="binding_error",
                message="required input field is missing",
                detail=f"missing fields: {missing_required}",
            )

        # ---- Execute with optional retry ----
        n_corpora = len(step.target_corpus_ids)
        max_attempts = (
            2
            if step.max_tool_calls >= n_corpora * 2
            else 1
            if step.max_tool_calls >= n_corpora
            else 0
        )

        attempts = 0
        last_exception: Exception | None = None

        while attempts < max_attempts:
            # Reserve budget.
            if not budget.try_reserve(n_corpora):
                return StepResult(
                    step_id=step_id,
                    status=StepStatus.budget_exhausted,
                    message="tool-call budget exhausted",
                    detail=f"attempt {attempts + 1} failed to reserve {n_corpora} units",
                    attempts=attempts,
                    tool_calls_consumed=attempts * n_corpora,
                )

            attempts += 1

            try:
                output = self._run_tool_with_timeout(tool, step, resolved_inputs, ctx, spec)

                # ---- Validate output against ToolSpec.output_models ----
                output_model = spec.output_models.get(step.output_schema_id)
                if output_model is None:
                    raise PlanExecutionError(
                        f"output schema {step.output_schema_id} not registered",
                        error_code="output_schema_error",
                        detail=f"step={step_id} schema_id={step.output_schema_id}",
                    )
                try:
                    validated = output_model.model_validate(output.outputs)
                except Exception as exc:
                    # Schema mismatch — non-retryable programming error.
                    return StepResult(
                        step_id=step_id,
                        status=StepStatus.failed,
                        error_code="output_schema_error",
                        message="step output failed schema validation",
                        detail=f"schema_id={step.output_schema_id} validation error: {exc}",
                        attempts=attempts,
                        tool_calls_consumed=attempts * n_corpora,
                    )

                return StepResult(
                    step_id=step_id,
                    status=StepStatus.succeeded,
                    outputs=validated.model_dump(),
                    evidence_ids=output.evidence_ids,
                    attempts=attempts,
                    tool_calls_consumed=attempts * n_corpora,
                )
            except _SECURITY_BINDING_ERRORS as exc:
                # Security binding failure — whole-execution fail-closed.
                raise PlanExecutionError(
                    "security binding failure during execution",
                    error_code="security_binding_failure",
                    detail=f"step={step_id} {type(exc).__name__}: {exc}",
                ) from exc
            except _PROGRAMMING_ERRORS as exc:
                # Programming error — not retried.
                return StepResult(
                    step_id=step_id,
                    status=StepStatus.failed,
                    error_code="programming_error",
                    message="a programming error occurred during execution",
                    detail=f"{type(exc).__name__}: {exc}",
                    attempts=attempts,
                    tool_calls_consumed=attempts * n_corpora,
                )
            except tuple(spec.retryable_errors) as exc:
                # Retryable error — will retry if attempts remain.
                last_exception = exc
                continue  # while loop retries
            except PlanExecutionError:
                raise  # output schema not registered (fail-closed)
            except TimeoutError as exc:
                # Step-level timeout (from _run_tool_with_timeout).
                if attempts < max_attempts and type(exc) in spec.retryable_errors:
                    last_exception = exc
                    continue  # retry
                return StepResult(
                    step_id=step_id,
                    status=StepStatus.timed_out,
                    message="step timed out",
                    detail=f"{type(exc).__name__}: {exc}",
                    attempts=attempts,
                    tool_calls_consumed=attempts * n_corpora,
                )
            except Exception as exc:
                # Any other exception — terminal failed, not retried.
                return StepResult(
                    step_id=step_id,
                    status=StepStatus.failed,
                    error_code="execution_error",
                    message="step execution failed",
                    detail=f"{type(exc).__name__}: {exc}",
                    attempts=attempts,
                    tool_calls_consumed=attempts * n_corpora,
                )

        # Exhausted retries.
        error_code = "retry_exhausted" if last_exception else "execution_error"
        return StepResult(
            step_id=step_id,
            status=StepStatus.failed,
            error_code=error_code,
            message="step failed after all retry attempts",
            detail=f"last error: {last_exception}" if last_exception else "",
            attempts=attempts,
            tool_calls_consumed=attempts * n_corpora,
        )

    # ------------------------------------------------------------------
    # Tool invocation with timeout
    # ------------------------------------------------------------------

    @staticmethod
    def _run_tool_with_timeout(
        tool: Tool,
        step: PlanStep,
        resolved_inputs: dict[str, object],
        ctx: SecurityContext,
        spec: ToolSpec,
    ) -> Any:
        """Run ``tool.execute_step`` with a timeout.

        Returns ``TypedStepOutput``.  On timeout the underlying worker thread is
        **not** waited for — the future is abandoned so a slow Tool does not
        block the executor beyond the deadline.
        """
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        future = pool.submit(tool.execute_step, step, resolved_inputs, ctx)
        try:
            return future.result(timeout=step.timeout_seconds)
        except concurrent.futures.TimeoutError:
            # Discard the late result: do NOT wait for the worker.
            pool.shutdown(wait=False)
            raise TimeoutError(
                f"step {step.step_id} timed out after {step.timeout_seconds}s"
            ) from None

    # ------------------------------------------------------------------
    # Result building
    # ------------------------------------------------------------------

    def _build_result(
        self,
        plan: QueryPlan,
        results: dict[str, StepResult],
        any_tool_launched: bool,
        limitations: list[str],
        budget: AtomicToolBudget,
    ) -> PlanExecutionResult:
        steps_in_order = _ordered_steps(plan, results)

        all_evidence_ids: list[str] = []
        seen_ids: set[str] = set()
        any_evidence = False

        for sr in steps_in_order:
            if sr.status == StepStatus.succeeded:
                for eid in sr.evidence_ids:
                    if eid not in seen_ids:
                        seen_ids.add(eid)
                        all_evidence_ids.append(eid)
                        any_evidence = True

        total_tool_calls = budget.used()
        degraded = any(s.status != StepStatus.succeeded for s in steps_in_order) and any_evidence

        if not any_evidence:
            raise PlanExecutionError(
                "no step produced usable evidence",
                error_code="no_usable_evidence",
            )

        return PlanExecutionResult(
            plan_id=plan.plan_id,
            accepted=True,
            executed=any_tool_launched,
            degraded=degraded,
            steps=tuple(steps_in_order),
            tool_calls_used=total_tool_calls,
            evidence_ids=tuple(all_evidence_ids),
            limitations=tuple(limitations),
        )


# ==================================================================
# Module-level helpers
# ==================================================================


def _build_topological_layers(plan: QueryPlan) -> list[list[str]]:
    """Partition steps into topological layers for parallel execution.

    Layer 0 = steps with zero dependencies (hard + optional).
    Layer N = steps whose all dependencies are in layers < N.
    """
    step_ids = plan.step_ids()

    # Compute in-degree (number of unmet dependencies) for each step.
    indeg: dict[str, int] = {}
    for step in plan.steps:
        deps = len(step.all_dependency_ids())
        indeg[step.step_id] = deps

    # Layer 0: steps with zero deps.
    layers: list[list[str]] = []
    current = [sid for sid in step_ids if indeg[sid] == 0]
    # Maintain original plan order for determinism within each layer.
    current.sort(key=lambda sid: list(step_ids).index(sid))

    while current:
        layers.append(current)
        next_layer: list[str] = []
        for sid in current:
            # Find all steps that depend on sid.
            for step in plan.steps:
                if sid in step.all_dependency_ids():
                    indeg[step.step_id] -= 1
                    if indeg[step.step_id] == 0:
                        next_layer.append(step.step_id)
        # Deterministic order for next layer.
        next_layer.sort(key=lambda sid: list(step_ids).index(sid))
        current = next_layer

    return layers


def _ordered_steps(
    plan: QueryPlan,
    results: dict[str, StepResult],
) -> list[StepResult]:
    """Return StepResults in the original plan / topological order."""
    # Map step_id -> results, preserving plan declaration order.
    return [results[s.step_id] for s in plan.steps if s.step_id in results]


def _resolve_inputs(
    step: PlanStep,
    completed: dict[str, StepResult],
    fact_values: dict[str, str],
    spec: ToolSpec,
) -> dict[str, object]:
    """Resolve bindings and template for a step.

    Returns:
        A dict containing the resolved query under ``__query__`` and all
        binding field names under their declared keys.
    """
    resolved: dict[str, object] = {}

    # Resolve input_bindings.
    for field_name, raw_expr in step.input_bindings.items():
        expr = BindingExpression.parse(raw_expr)
        if expr.kind == BindingKind.STEP_OUTPUT:
            assert expr.step_id is not None
            assert expr.output_field is not None
            upstream = completed.get(expr.step_id)
            if upstream is None:
                raise ValueError(
                    f"binding {field_name}: upstream step {expr.step_id} not completed"
                )
            value = upstream.outputs.get(expr.output_field)
            if value is None:
                # Check if the field is required in input_model.
                field_info = spec.input_model.model_fields.get(field_name)
                if field_info and field_info.is_required():
                    raise ValueError(
                        f"binding {field_name}: required field {expr.output_field} is missing"
                    )
                # Optional field — leave as None/default.
            resolved[field_name] = value
        elif expr.kind == BindingKind.FACT_VALUE:
            assert expr.fact_id is not None
            value = fact_values.get(expr.fact_id)
            if value is None:
                raise ValueError(f"binding {field_name}: fact {expr.fact_id} not found")
            resolved[field_name] = value

    # Resolve query / query_template.
    query = _resolve_query(step, completed)
    resolved[_RESOLVED_QUERY_KEY] = query

    return resolved


def _resolve_query(
    step: PlanStep,
    completed: dict[str, StepResult],
) -> str:
    """Resolve the step query, substituting template placeholders."""
    if step.query is not None:
        return step.query
    if step.query_template is not None:
        return _substitute_template(step.query_template, completed)
    raise ValueError("step has no query or query_template")


def _substitute_template(
    template: str,
    completed: dict[str, StepResult],
) -> str:
    """Replace ``{{step_id.field}}`` placeholders with upstream output values."""

    def _replacer(match: re.Match[str]) -> str:
        placeholder = match.group(0)
        m = _TMPL_PARSE_RE.match(placeholder)
        if not m:
            raise ValueError(f"malformed template placeholder: {placeholder}")
        step_id, field = m.group(1), m.group(2)
        upstream = completed.get(step_id)
        if upstream is None:
            raise ValueError(f"template references step {step_id} which is not completed")
        value = upstream.outputs.get(field)
        if value is None:
            raise ValueError(
                f"template references field {field} on step {step_id} which is not in outputs"
            )
        return str(value)

    return _TMPL_FIND_RE.sub(_replacer, template)


def _check_missing_required(
    model_class: type[BaseModel],
    resolved_inputs: dict[str, object],
) -> list[str]:
    """Check which required input_model fields are missing from resolved_inputs.

    Uses Pydantic ``is_required()`` to determine requiredness (contract §4a).
    The ``__query__`` internal key is excluded from this check.
    """
    missing: list[str] = []
    for field_name, field_info in model_class.model_fields.items():
        if field_name == _RESOLVED_QUERY_KEY:
            continue
        if field_info.is_required() and field_name not in resolved_inputs:
            missing.append(field_name)
    return missing
