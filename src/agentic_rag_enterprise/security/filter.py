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


def build_access_filter(ctx: SecurityContext, corpus_id: str) -> Filter:
    """Build a Qdrant ``Filter`` enforcing the access truth table.

    Encodes: tenant match, active status, allowed security levels, and the
    tenant/restricted scope allow/deny logic, with deny precedence.
    """

    must: list[Condition] = [
        FieldCondition(key="tenant_id", match=MatchValue(value=ctx.tenant_id)),
        FieldCondition(key="corpus_id", match=MatchValue(value=corpus_id)),
        FieldCondition(key="status", match=MatchValue(value="active")),
        FieldCondition(key="deprecated", match=MatchValue(value=False)),
        FieldCondition(
            key="security_level",
            match=MatchAny(any=list(ctx.allowed_security_levels)),
        ),
        Filter(
            should=[
                FieldCondition(key="acl_scope", match=MatchValue(value="tenant")),
                FieldCondition(
                    key="allowed_user_ids",
                    match=MatchAny(any=[ctx.user_id]),
                ),
                FieldCondition(
                    key="allowed_group_ids",
                    match=MatchAny(any=list(ctx.groups)),
                ),
            ],
        ),
    ]

    must_not: list[Condition] = [
        FieldCondition(
            key="denied_user_ids",
            match=MatchAny(any=[ctx.user_id]),
        ),
        FieldCondition(
            key="denied_group_ids",
            match=MatchAny(any=list(ctx.groups)),
        ),
    ]

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
