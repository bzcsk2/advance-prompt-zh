"""Policy decision point (PDP) and the access truth table.

The truth table in :func:`evaluate_access` is the single source of authorization
logic for retrieval-time access. Qdrant filters (PEP) must be derived from
this decision, never from a model-chosen corpus list or free-form document
metadata.

Fixed ACL semantics (build plan §11.3):

* ``deny`` always precedes ``allow``, including for admins. Admin bypass is
  only possible through an independent, auditable break-glass policy.
* ``acl_scope == "tenant"`` means every user in the tenant whose
  ``security_level`` is allowed can read the resource.
* ``acl_scope == "restricted"`` with empty allow lists means nobody can read
  it — it does **not** mean public.
* Corpus discoverability and document readability are computed separately;
  the former never substitutes for the latter.
* ``is_admin`` grants no implicit read permission on its own.
"""

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field

from agentic_rag_enterprise.domain.document import SourceDocument
from agentic_rag_enterprise.domain.security import SecurityContext


class AuthorizationDecision(str, Enum):
    ALLOW = "allow"
    DENY = "deny"


class ResourceAcl(BaseModel):
    """ACL attributes a protected resource carries."""

    tenant_id: str
    security_level: str

    acl_scope: Literal["tenant", "restricted"] = "restricted"
    allowed_user_ids: list[str] = Field(default_factory=list)
    allowed_group_ids: list[str] = Field(default_factory=list)
    denied_user_ids: list[str] = Field(default_factory=list)
    denied_group_ids: list[str] = Field(default_factory=list)


def evaluate_access(ctx: SecurityContext, acl: ResourceAcl) -> AuthorizationDecision:
    """Return the canonical allow/deny decision for ``ctx`` on ``acl``.

    Evaluation order is fixed and intentional:
    1. Tenant boundary (hard stop, before anything else).
    2. Deny precedence (user then group), including admins.
    3. Security-level gate.
    4. Scope-based allow (tenant scope, else explicit allow lists).
    """

    # 1. Tenant boundary is the first filter.
    if ctx.tenant_id != acl.tenant_id:
        return AuthorizationDecision.DENY

    # 2. Deny always precedes allow, even for admins.
    if ctx.user_id in acl.denied_user_ids:
        return AuthorizationDecision.DENY
    if set(ctx.groups) & set(acl.denied_group_ids):
        return AuthorizationDecision.DENY

    # 3. Security-level gate.
    if acl.security_level not in ctx.allowed_security_levels:
        return AuthorizationDecision.DENY

    # 4. Scope-based allow.
    if acl.acl_scope == "tenant":
        return AuthorizationDecision.ALLOW

    # acl_scope == "restricted": empty allow lists mean nobody, not public.
    if ctx.user_id in acl.allowed_user_ids:
        return AuthorizationDecision.ALLOW
    if set(ctx.groups) & set(acl.allowed_group_ids):
        return AuthorizationDecision.ALLOW
    return AuthorizationDecision.DENY


def can_discover_corpus(ctx: SecurityContext, corpus_id: str) -> bool:
    """Corpus discoverability is separate from document readability.

    ``None`` ``allowed_corpus_ids`` means "all corpora the runtime knows
    about"; an explicit list restricts discovery to those ids.
    """

    if ctx.allowed_corpus_ids is None:
        return True
    return corpus_id in ctx.allowed_corpus_ids


def can_manage_document(ctx: SecurityContext, doc: SourceDocument) -> bool:
    """Write-authorization for document mutation (update/delete/purge/ACL).

    Fail-closed: a caller may mutate a document only if it is in their tenant,
    the corpus is discoverable to them, AND they can already read the document
    (the canonical PDP allows the document's ACL for ``ctx``). Cross-tenant and
    non-discoverable corpora are denied outright (build plan §10.6/§10.7).
    """
    if ctx.tenant_id != doc.tenant_id:
        return False
    if not can_discover_corpus(ctx, doc.corpus_id):
        return False
    acl = ResourceAcl(
        tenant_id=doc.tenant_id,
        security_level=doc.security_level,
        acl_scope=doc.acl_scope,
        allowed_user_ids=doc.allowed_user_ids,
        allowed_group_ids=doc.allowed_group_ids,
        denied_user_ids=doc.denied_user_ids,
        denied_group_ids=doc.denied_group_ids,
    )
    return evaluate_access(ctx, acl) is AuthorizationDecision.ALLOW


class AccessPolicy:
    """Retrieval-time access control.

    Prefer :func:`evaluate_access` for the canonical truth table. ``decide``
    is a convenience boolean wrapper. ``is_admin`` grants no implicit read.
    """

    def decide(self, ctx: SecurityContext, acl: ResourceAcl) -> bool:
        return evaluate_access(ctx, acl) is AuthorizationDecision.ALLOW

    # Backward-compatible shim for the corpus-level ``allowed_users`` policy
    # exercised by the M0 baseline characterization tests. Superseded by
    # :func:`evaluate_access`; remove once those tests migrate.
    def can_access(self, user_id: str, corpus: Any) -> bool:
        allowed = (getattr(corpus, "access_policy", None) or {}).get("allowed_users")
        if not allowed:
            return True
        return user_id in allowed
