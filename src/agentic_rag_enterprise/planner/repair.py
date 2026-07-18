"""E-017 Planner structured-output repair (build plan §13.3).

The Planner output is untrusted: it may be a malformed dict / JSON string. We parse it
into the frozen :class:`QueryPlan`; on the **first** schema failure we allow exactly one
injected ``repair_fn`` to correct it, then re-parse. A failure after the repair raises
:class:`PlanRepairExhaustedError` (§13.3: "禁止无限修复 Planner 输出").

This module is pure: it performs *no* retrieval and *no* validation. Parsing the raw
output into a typed plan and validating it are two separate concerns — the caller
validates the returned plan with :class:`~agentic_rag_enterprise.planner.validator.
PlanValidator`.
"""

from __future__ import annotations

import json
from typing import Callable

from pydantic import ValidationError

from agentic_rag_enterprise.planner.models import QueryPlan


class PlanRepairExhaustedError(Exception):
    """Raised when the Planner output is still invalid after the single allowed repair."""


def parse_plan(
    raw: dict | str,
    *,
    repair_fn: Callable[[dict], dict],
) -> QueryPlan:
    """Parse untrusted Planner output into a :class:`QueryPlan`.

    :param raw: a dict or JSON string produced by the Planner.
    :param repair_fn: called at most once (on the first schema error) with the parsed
        dict and expected to return a corrected dict.
    :raises PlanRepairExhaustedError: if the repaired output still fails schema
        validation (no second repair is attempted).
    :raises ValidationError: propagated if ``raw`` itself is not dict/str-parseable
        JSON (that is a transport error, not a repairable schema error).
    """
    data = _coerce(raw)

    def _attempt(payload: dict) -> QueryPlan:
        return QueryPlan.model_validate(payload)

    try:
        return _attempt(data)
    except ValidationError:
        pass

    # exactly one repair attempt
    repaired = repair_fn(data)
    if not isinstance(repaired, dict):
        raise PlanRepairExhaustedError(
            "repair_fn did not return a dict; Planner output still invalid"
        )
    try:
        return _attempt(repaired)
    except ValidationError as exc:
        raise PlanRepairExhaustedError(
            "Planner output still invalid after the single allowed repair"
        ) from exc


def _coerce(raw: dict | str) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        return json.loads(raw)
    raise ValidationError.from_exception_data(
        "QueryPlan",
        [
            {
                "type": "value_error",
                "loc": ("raw",),
                "input": raw,
                "ctx": {"error": "expected dict or JSON string"},
            }
        ],
    )
