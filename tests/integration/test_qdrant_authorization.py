"""Integration test: Qdrant Filter (PEP) must equal the PDP truth table.

This is the production proof for E-006.1 P1-3. It builds a real in-memory
Qdrant collection of resources with a controlled ACL/payload matrix, derives
the retrieval filter with :func:`build_access_filter`, runs it via ``scroll``,
and asserts the returned point ids are exactly the set the PDP
:func:`evaluate_access` allows (for active, non-deprecated, same-tenant
resources in the target corpus).
"""

from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

from agentic_rag_enterprise.domain.security import SecurityContext
from agentic_rag_enterprise.security.filter import build_access_filter
from agentic_rag_enterprise.security.policy import (
    AuthorizationDecision,
    evaluate_access,
    ResourceAcl,
)

CORPUS_ID = "engineering_wiki"
TENANT_ID = "t1"
VECTOR_SIZE = 4


def _ctx() -> SecurityContext:
    return SecurityContext(
        request_id="r",
        session_id="s",
        tenant_id=TENANT_ID,
        user_id="u1",
        groups=["g1"],
        allowed_security_levels=["public", "internal"],
        allowed_corpus_ids=None,
        policy_version="1.0",
        is_admin=False,
    )


# Each entry: (point_id, payload). status/deprecated default to active/False.
_RESOURCES: list[tuple[int, dict]] = [
    (
        1,
        {
            "tenant_id": TENANT_ID,
            "corpus_id": CORPUS_ID,
            "acl_scope": "tenant",
            "security_level": "public",
        },
    ),
    (
        2,
        {
            "tenant_id": TENANT_ID,
            "corpus_id": CORPUS_ID,
            "acl_scope": "tenant",
            "security_level": "confidential",
        },
    ),
    (
        3,
        {
            "tenant_id": TENANT_ID,
            "corpus_id": CORPUS_ID,
            "acl_scope": "restricted",
            "security_level": "public",
        },
    ),
    (
        4,
        {
            "tenant_id": TENANT_ID,
            "corpus_id": CORPUS_ID,
            "acl_scope": "restricted",
            "security_level": "public",
            "allowed_user_ids": ["u1"],
        },
    ),
    (
        5,
        {
            "tenant_id": TENANT_ID,
            "corpus_id": CORPUS_ID,
            "acl_scope": "restricted",
            "security_level": "public",
            "allowed_group_ids": ["g1"],
        },
    ),
    (
        6,
        {
            "tenant_id": TENANT_ID,
            "corpus_id": CORPUS_ID,
            "acl_scope": "restricted",
            "security_level": "public",
            "allowed_user_ids": ["u1"],
            "denied_user_ids": ["u1"],
        },
    ),
    (
        7,
        {
            "tenant_id": TENANT_ID,
            "corpus_id": CORPUS_ID,
            "acl_scope": "restricted",
            "security_level": "public",
            "allowed_group_ids": ["g1"],
            "denied_group_ids": ["g1"],
        },
    ),
    (
        8,
        {
            "tenant_id": "t2",
            "corpus_id": CORPUS_ID,
            "acl_scope": "tenant",
            "security_level": "public",
        },
    ),
    (
        9,
        {
            "tenant_id": TENANT_ID,
            "corpus_id": CORPUS_ID,
            "acl_scope": "restricted",
            "security_level": "confidential",
            "allowed_user_ids": ["u1"],
        },
    ),
    (
        10,
        {
            "tenant_id": TENANT_ID,
            "corpus_id": CORPUS_ID,
            "acl_scope": "tenant",
            "security_level": "public",
            "status": "deleted",
        },
    ),
    (
        11,
        {
            "tenant_id": TENANT_ID,
            "corpus_id": CORPUS_ID,
            "acl_scope": "tenant",
            "security_level": "public",
            "deprecated": True,
        },
    ),
    (
        12,
        {
            "tenant_id": TENANT_ID,
            "corpus_id": "other_corpus",
            "acl_scope": "tenant",
            "security_level": "public",
        },
    ),
]


def _make_points() -> list[PointStruct]:
    points: list[PointStruct] = []
    for pid, payload in _RESOURCES:
        full = {
            "tenant_id": TENANT_ID,
            "corpus_id": CORPUS_ID,
            "status": "active",
            "deprecated": False,
            "acl_scope": "restricted",
            "security_level": "public",
            "allowed_user_ids": [],
            "allowed_group_ids": [],
            "denied_user_ids": [],
            "denied_group_ids": [],
        }
        full.update(payload)
        points.append(PointStruct(id=pid, vector=[float(pid)] * VECTOR_SIZE, payload=full))
    return points


def _expected_allowed(ctx: SecurityContext) -> set[int]:
    allowed: set[int] = set()
    for pid, payload in _RESOURCES:
        if (
            payload.get("tenant_id", TENANT_ID) != ctx.tenant_id
            or payload.get("corpus_id", CORPUS_ID) != CORPUS_ID
            or payload.get("status", "active") != "active"
            or payload.get("deprecated", False)
        ):
            continue
        acl = ResourceAcl(
            tenant_id=payload.get("tenant_id", TENANT_ID),
            security_level=payload["security_level"],
            acl_scope=payload.get("acl_scope", "restricted"),
            allowed_user_ids=payload.get("allowed_user_ids", []),
            allowed_group_ids=payload.get("allowed_group_ids", []),
            denied_user_ids=payload.get("denied_user_ids", []),
            denied_group_ids=payload.get("denied_group_ids", []),
        )
        if evaluate_access(ctx, acl) is AuthorizationDecision.ALLOW:
            allowed.add(pid)
    return allowed


def _actual_allowed(ctx: SecurityContext) -> set[int]:
    client = QdrantClient(location=":memory:")
    client.create_collection(
        collection_name="authz",
        vectors_config=VectorParams(size=VECTOR_SIZE, distance=Distance.COSINE),
    )
    client.upsert(collection_name="authz", points=_make_points())

    flt = build_access_filter(ctx, CORPUS_ID)
    points, _ = client.scroll(collection_name="authz", scroll_filter=flt, limit=1000)
    client.close()
    return {p.id for p in points}


def test_qdrant_filter_matches_pdp() -> None:
    ctx = _ctx()
    actual = _actual_allowed(ctx)
    expected = _expected_allowed(ctx)
    assert actual == expected


def test_qdrant_filter_allows_tenant_scope_public() -> None:
    ctx = _ctx()
    assert 1 in _actual_allowed(ctx)


def test_qdrant_filter_denies_security_level() -> None:
    ctx = _ctx()
    assert 2 not in _actual_allowed(ctx)


def test_qdrant_filter_denies_restricted_empty() -> None:
    ctx = _ctx()
    assert 3 not in _actual_allowed(ctx)


def test_qdrant_filter_allows_user_allow() -> None:
    ctx = _ctx()
    assert 4 in _actual_allowed(ctx)


def test_qdrant_filter_allows_group_allow() -> None:
    ctx = _ctx()
    assert 5 in _actual_allowed(ctx)


def test_qdrant_filter_user_deny_override() -> None:
    ctx = _ctx()
    assert 6 not in _actual_allowed(ctx)


def test_qdrant_filter_group_deny_override() -> None:
    ctx = _ctx()
    assert 7 not in _actual_allowed(ctx)


def test_qdrant_filter_denies_cross_tenant() -> None:
    ctx = _ctx()
    assert 8 not in _actual_allowed(ctx)


def test_qdrant_filter_denies_security_level_even_when_user_allowed() -> None:
    ctx = _ctx()
    assert 9 not in _actual_allowed(ctx)


def test_qdrant_filter_denies_inactive() -> None:
    ctx = _ctx()
    assert 10 not in _actual_allowed(ctx)


def test_qdrant_filter_denies_deprecated() -> None:
    ctx = _ctx()
    assert 11 not in _actual_allowed(ctx)


def test_qdrant_filter_denies_other_corpus() -> None:
    ctx = _ctx()
    assert 12 not in _actual_allowed(ctx)
