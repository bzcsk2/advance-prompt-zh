"""Unit tests for the E-017 §13.2 binding grammar (planner/binding.py)."""

from __future__ import annotations

import pytest

from agentic_rag_enterprise.planner.binding import (
    BindingExpression,
    BindingKind,
    BindingSyntaxError,
)


def test_step_output_binding_parses() -> None:
    expr = BindingExpression.parse("steps.s1.outputs.field_a")
    assert expr.kind == BindingKind.STEP_OUTPUT
    assert expr.step_id == "s1"
    assert expr.output_field == "field_a"


def test_fact_value_binding_parses() -> None:
    expr = BindingExpression.parse("facts.F1.value")
    assert expr.kind == BindingKind.FACT_VALUE
    assert expr.fact_id == "F1"


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "  ",
        "s1.field_a",
        "steps.s1.field_a",  # missing 'outputs'
        "steps..outputs.x",
        "facts.F1",  # missing '.value'
        "steps.s1.outputs.x.y",  # too deep
        "{{s1.x}}",  # template text is not a binding value
        "steps.s1.outputs.x.value",  # extra token
    ],
)
def test_binding_rejects_arbitrary_expressions(bad: str) -> None:
    with pytest.raises(BindingSyntaxError):
        BindingExpression.parse(bad)


def test_template_placeholder_parses() -> None:
    expr = BindingExpression.parse_template_placeholder("{{s1.field_a}}")
    assert expr.kind == BindingKind.STEP_OUTPUT
    assert expr.step_id == "s1"
    assert expr.output_field == "field_a"
    # tolerates internal whitespace
    expr2 = BindingExpression.parse_template_placeholder("{{ s2.b }}")
    assert expr2.step_id == "s2" and expr2.output_field == "b"


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "s1.field_a",
        "{{s1}}",  # missing field
        "{{s1.field_a.extra}}",
        "{s1.field_a}",  # single brace
        "{{s1.}}",
    ],
)
def test_template_placeholder_rejects(bad: str) -> None:
    with pytest.raises(BindingSyntaxError):
        BindingExpression.parse_template_placeholder(bad)


def test_underscore_identifiers_allowed() -> None:
    expr = BindingExpression.parse("steps.find_server.outputs.server_id")
    assert expr.step_id == "find_server"
    assert expr.output_field == "server_id"
