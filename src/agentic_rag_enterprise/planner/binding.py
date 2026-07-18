"""E-017 Planner binding grammar (build plan §13.2).

The grammar is deliberately tiny and model-free:

* ``steps.<step_id>.outputs.<field>``  -> STEP_OUTPUT
* ``facts.<fact_id>.value``           -> FACT_VALUE

No arbitrary expressions, no Jinja, no Python, no ``{{...}}`` template text as a binding
value. A ``query_template`` uses pre-parsed ``{{step_id.field}}`` placeholders, which are
parsed by :func:`parse_template_placeholder` into the *same* ``STEP_OUTPUT`` expression —
the validator then checks the referenced step is a declared dependency.
"""

from __future__ import annotations

import re
from enum import Enum

from pydantic import BaseModel, ConfigDict

from agentic_rag_enterprise.planner.models import PlanViolationCode, PlanViolation


class BindingSyntaxError(ValueError):
    """Raised when a binding string does not match the §13.2 grammar."""


class BindingKind(str, Enum):
    STEP_OUTPUT = "step_output"
    FACT_VALUE = "fact_value"


_IDENT = r"[A-Za-z_][A-Za-z0-9_]*"
_STEP_RE = re.compile(rf"^steps\.({_IDENT})\.outputs\.({_IDENT})$")
_FACT_RE = re.compile(rf"^facts\.({_IDENT})\.value$")
# {{ step_id.field }}  or  {{step_id.field}}  — the surrounding braces are literal,
# not f-string escapes, so build the pattern outside an f-string with explicit groups.
_TMPL_RE = re.compile(r"^\{\{\s*(" + _IDENT + r")\.(" + _IDENT + r")\s*\}\}$")


class BindingExpression(BaseModel):
    """A parsed binding reference (frozen)."""

    model_config = ConfigDict(frozen=True)

    raw: str
    kind: BindingKind
    step_id: str | None = None  # when kind == STEP_OUTPUT
    output_field: str | None = None  # when kind == STEP_OUTPUT
    fact_id: str | None = None  # when kind == FACT_VALUE

    @classmethod
    def parse(cls, raw: str) -> "BindingExpression":
        """Parse a binding *value* string. Raise :class:`BindingSyntaxError` on failure."""
        if not isinstance(raw, str) or raw == "":
            raise BindingSyntaxError(f"empty binding value: {raw!r}")
        m = _STEP_RE.match(raw)
        if m:
            return cls(
                raw=raw,
                kind=BindingKind.STEP_OUTPUT,
                step_id=m.group(1),
                output_field=m.group(2),
            )
        m = _FACT_RE.match(raw)
        if m:
            return cls(raw=raw, kind=BindingKind.FACT_VALUE, fact_id=m.group(1))
        raise BindingSyntaxError(
            f"binding {raw!r} is not 'steps.<id>.outputs.<field>' or 'facts.<id>.value'"
        )

    @classmethod
    def parse_template_placeholder(cls, placeholder: str) -> "BindingExpression":
        """Parse a ``{{step_id.field}}`` template placeholder.

        Raises :class:`BindingSyntaxError` if the placeholder is malformed. Always yields
        a ``STEP_OUTPUT`` expression (template placeholders cannot reference facts).
        """
        if not isinstance(placeholder, str) or placeholder == "":
            raise BindingSyntaxError(f"empty template placeholder: {placeholder!r}")
        m = _TMPL_RE.match(placeholder.strip())
        if not m:
            raise BindingSyntaxError(
                f"template placeholder {placeholder!r} is not '{{{{step_id.field}}}}'"
            )
        return cls(
            raw=placeholder,
            kind=BindingKind.STEP_OUTPUT,
            step_id=m.group(1),
            output_field=m.group(2),
        )


def binding_violation(raw: str, step_id: str | None = None) -> PlanViolation:
    """Build a user-safe INVALID_BINDING violation for a malformed expression."""
    return PlanViolation.user_safe(
        PlanViolationCode.INVALID_BINDING,
        "plan contains a binding that does not reference a legal upstream step or fact",
        detail=raw,
        step_id=step_id,
    )
