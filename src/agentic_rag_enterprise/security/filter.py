"""Policy enforcement point (PEP): derive Qdrant filters from the PDP.

The filter encodes exactly the truth table in
:func:`agentic_rag_enterprise.security.policy.evaluate_access`. The model
never chooses which corpora to search or which ACL fields to trust; the
runtime computes the filter from the current :class:`SecurityContext` and
injects it into every retrieval call.
"""

from qdrant_client.models import Condition, FieldCondition, Filter, MatchAny, MatchValue

from agentic_rag_enterprise.domain.security import SecurityContext
from agentic_rag_enterprise.security.policy import (
    AuthorizationDecision,
    evaluate_access,
    ResourceAcl,
)


class EmptyAuthorizationScopeError(Exception):
    """Raised when the caller's authorization scope cannot authorize anything.

    An empty ``allowed_security_levels`` means the PDP would deny every
    resource, so we fail closed by raising here instead of constructing a filter
    a crafted resource payload could slip past. (Empty ``groups`` is NOT a
    deny-all: a tenant-scoped resource or an explicit user allow can still
    match, so an empty group set simply omits the group allow/deny branches.)
    """


def build_access_filter(ctx: SecurityContext, corpus_id: str) -> Filter:
    """Build a Qdrant ``Filter`` enforcing the access truth table.

    Encodes: tenant match, active status, allowed security levels, and the
    tenant/restricted scope allow/deny logic, with deny precedence.

    Fail-closed semantics (kept identical to :func:`evaluate_access`):

    * ``allowed_security_levels`` empty -> the PDP would deny everything, so we
      raise :class:`EmptyAuthorizationScopeError` instead of producing a filter
      that a crafted resource could slip past.
    * ``groups`` empty -> there is simply no group branch (no allow, no deny),
      matching the PDP where ``set(ctx.groups)`` is empty.
    """
    levels = list(ctx.allowed_security_levels)
    if not levels:
        raise EmptyAuthorizationScopeError(
            "allowed_security_levels is empty; denying all retrieval"
        )
    groups = list(ctx.groups)

    security_level_cond: Condition = FieldCondition(
        key="security_level", match=MatchAny(any=levels)
    )

    scope_conditions: list[Condition] = [
        FieldCondition(key="acl_scope", match=MatchValue(value="tenant")),
        FieldCondition(
            key="allowed_user_ids",
            match=MatchAny(any=[ctx.user_id]),
        ),
    ]
    if groups:
        scope_conditions.append(
            FieldCondition(
                key="allowed_group_ids",
                match=MatchAny(any=groups),
            )
        )

    must: list[Condition] = [
        FieldCondition(key="tenant_id", match=MatchValue(value=ctx.tenant_id)),
        FieldCondition(key="corpus_id", match=MatchValue(value=corpus_id)),
        FieldCondition(key="status", match=MatchValue(value="active")),
        FieldCondition(key="deprecated", match=MatchValue(value=False)),
        security_level_cond,
        Filter(should=scope_conditions),
    ]

    must_not: list[Condition] = [
        FieldCondition(
            key="denied_user_ids",
            match=MatchAny(any=[ctx.user_id]),
        ),
    ]
    if groups:
        must_not.append(
            FieldCondition(
                key="denied_group_ids",
                match=MatchAny(any=groups),
            )
        )

    return Filter(must=must, must_not=must_not)


def resource_passes_filter(
    ctx: SecurityContext,
    acl: ResourceAcl,
    status: str = "active",
    deprecated: bool = False,
) -> bool:
    """Cheap, Qdrant-free projection of :func:`build_access_filter`.

    Useful for pre-flight checks (e.g. parent-store second-pass
    authorization) where the resource is already loaded. A deprecated or
    non-active resource never passes, mirroring the Qdrant ``must`` filter.
    """

    if status != "active" or deprecated:
        return False
    return evaluate_access(ctx, acl) is AuthorizationDecision.ALLOW
