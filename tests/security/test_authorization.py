"""Authorization tests: ACL truth table, corpus discoverability, and PEP filter.

These are the spec-required truth-table unit tests (build plan §11.3)
covering public, restricted, user allow, group allow, deny override,
empty ACL, and cross-tenant scenarios.
"""

import pytest

from agentic_rag_enterprise.domain.security import SecurityContext
from agentic_rag_enterprise.security.filter import (
    build_access_filter,
    resource_passes_filter,
)
from qdrant_client.models import FieldCondition, Filter
from agentic_rag_enterprise.security.policy import (
    AuthorizationDecision,
    can_discover_corpus,
    evaluate_access,
    ResourceAcl,
)


def _ctx(**overrides: object) -> SecurityContext:
    base: dict = dict(
        request_id="r",
        session_id="s",
        tenant_id="t1",
        user_id="u1",
        policy_version="1.0",
    )
    base.update(overrides)  # type: ignore[arg-type]
    return SecurityContext(**base)


def _acl(**overrides: object) -> ResourceAcl:
    base: dict = dict(tenant_id="t1", security_level="public")
    base.update(overrides)  # type: ignore[arg-type]
    return ResourceAcl(**base)


# =============================================================================
# Access truth table (evaluate_access)
# =============================================================================

_TRUTH_TABLE = [
    # ctx overrides, acl overrides, expected
    ({}, {"acl_scope": "tenant"}, "allow"),
    ({}, {"acl_scope": "tenant", "security_level": "confidential"}, "deny"),
    # Real cross-tenant: tenant boundary is a hard stop, regardless of scope.
    ({"tenant_id": "t2"}, {"tenant_id": "t1", "acl_scope": "tenant"}, "deny"),
    (
        {"tenant_id": "t2"},
        {"tenant_id": "t1", "acl_scope": "tenant", "security_level": "public"},
        "deny",
    ),
    # Cross-tenant with an explicit allow cannot bypass the tenant boundary.
    (
        {"tenant_id": "t2"},
        {"tenant_id": "t1", "acl_scope": "restricted", "allowed_user_ids": ["u1"]},
        "deny",
    ),
    # Cross-tenant admin gets no implicit bypass either.
    (
        {"tenant_id": "t2", "is_admin": True},
        {"tenant_id": "t1", "acl_scope": "tenant"},
        "deny",
    ),
    ({}, {"acl_scope": "restricted"}, "deny"),
    ({}, {"acl_scope": "restricted", "allowed_user_ids": ["u1"]}, "allow"),
    ({}, {"acl_scope": "restricted", "allowed_group_ids": ["g1"]}, "deny"),
    ({"groups": ["g1"]}, {"acl_scope": "restricted", "allowed_group_ids": ["g1"]}, "allow"),
    (
        {},
        {"acl_scope": "restricted", "allowed_user_ids": ["u1"], "denied_user_ids": ["u1"]},
        "deny",
    ),
    (
        {"groups": ["g1"]},
        {"acl_scope": "restricted", "allowed_group_ids": ["g1"], "denied_group_ids": ["g1"]},
        "deny",
    ),
    ({"is_admin": True}, {"acl_scope": "restricted"}, "deny"),
    ({"is_admin": True}, {"acl_scope": "tenant", "denied_user_ids": ["u1"]}, "deny"),
    (
        {},
        {"acl_scope": "restricted", "security_level": "confidential", "allowed_user_ids": ["u1"]},
        "deny",
    ),
]


@pytest.mark.parametrize(("ctx_kw", "acl_kw", "expected"), _TRUTH_TABLE)
def test_evaluate_access_truth_table(ctx_kw: dict, acl_kw: dict, expected: str) -> None:
    ctx = _ctx(**ctx_kw)
    acl = _acl(**acl_kw)
    got = evaluate_access(ctx, acl)
    assert got.value == expected


def test_evaluate_access_returns_enum() -> None:
    ctx = _ctx()
    acl = _acl(acl_scope="tenant")
    assert evaluate_access(ctx, acl) is AuthorizationDecision.ALLOW


def test_access_policy_decide_bool() -> None:
    from agentic_rag_enterprise.security.policy import AccessPolicy

    policy = AccessPolicy()
    assert policy.decide(_ctx(), _acl(acl_scope="tenant")) is True
    assert policy.decide(_ctx(), _acl(acl_scope="restricted")) is False


def test_is_admin_does_not_bypass_restricted() -> None:
    ctx = _ctx(is_admin=True)
    acl = _acl(acl_scope="restricted", allowed_user_ids=["someone_else"])
    assert evaluate_access(ctx, acl) is AuthorizationDecision.DENY


def test_tenant_scope_ignores_allow_lists() -> None:
    ctx = _ctx()
    acl = _acl(acl_scope="tenant", allowed_user_ids=[], allowed_group_ids=[])
    assert evaluate_access(ctx, acl) is AuthorizationDecision.ALLOW


# =============================================================================
# Corpus discoverability (separate from document readability)
# =============================================================================


def test_corpus_discoverable_when_unrestricted() -> None:
    ctx = _ctx(allowed_corpus_ids=None)
    assert can_discover_corpus(ctx, "engineering_wiki") is True


def test_corpus_discoverable_in_list() -> None:
    ctx = _ctx(allowed_corpus_ids=["product_docs", "engineering_wiki"])
    assert can_discover_corpus(ctx, "engineering_wiki") is True


def test_corpus_not_discoverable_outside_list() -> None:
    ctx = _ctx(allowed_corpus_ids=["product_docs"])
    assert can_discover_corpus(ctx, "engineering_wiki") is False


# =============================================================================
# PEP: Qdrant filter derived from the truth table
# =============================================================================


def test_build_access_filter_structure() -> None:
    ctx = _ctx(groups=["g1"])
    flt = build_access_filter(ctx, "engineering_wiki")

    assert flt.must is not None
    assert flt.must_not is not None
    assert len(flt.must) == 6
    assert len(flt.must_not) == 2

    field_conditions = [c for c in flt.must if isinstance(c, FieldCondition)]
    keys = [c.key for c in field_conditions]
    assert "tenant_id" in keys
    assert "corpus_id" in keys
    assert "status" in keys
    assert "deprecated" in keys
    assert "security_level" in keys

    # scope OR-filter is the 6th must condition
    scope_filter = flt.must[5]
    assert isinstance(scope_filter, Filter)
    assert len(scope_filter.should) == 3

    must_not_keys = [c.key for c in flt.must_not]
    assert "denied_user_ids" in must_not_keys
    assert "denied_group_ids" in must_not_keys


def test_build_access_filter_injects_identity() -> None:
    ctx = _ctx(tenant_id="t9", user_id="u9", groups=["g1"])
    flt = build_access_filter(ctx, "corpus_x")

    by_key = {c.key: c for c in flt.must if isinstance(c, FieldCondition)}
    assert by_key["tenant_id"].match.value == "t9"  # type: ignore[union-attr]
    assert by_key["corpus_id"].match.value == "corpus_x"  # type: ignore[union-attr]


def test_resource_passes_filter_mirrors_truth_table() -> None:
    ctx = _ctx(groups=["g1"])
    acl = _acl(acl_scope="restricted", allowed_group_ids=["g1"])
    assert resource_passes_filter(ctx, acl) is True

    acl_denied = _acl(
        acl_scope="restricted",
        allowed_group_ids=["g1"],
        denied_group_ids=["g1"],
    )
    assert resource_passes_filter(ctx, acl_denied) is False


def test_resource_passes_filter_requires_active() -> None:
    ctx = _ctx()
    acl = _acl(acl_scope="tenant")
    assert resource_passes_filter(ctx, acl, status="deleted") is False


def test_resource_passes_filter_rejects_deprecated() -> None:
    ctx = _ctx()
    acl = _acl(acl_scope="tenant")
    assert resource_passes_filter(ctx, acl, deprecated=True) is False
