"""Integration test: Qdrant hybrid retrieval must equal the PDP truth table.

Reuses the E-006.1 ACL matrix but routes it through the E-007
:class:`_HybridSearchAdapter` + :class:`VectorStore` (real in-memory Qdrant,
dense+sparse RRF fusion, mandatory ``build_access_filter``). Also covers the
mandatory **corpus discoverability** gate and the empty-``groups`` /
empty-``allowed_security_levels`` fail-closed cases.
"""

from datetime import datetime

from qdrant_client import QdrantClient

from agentic_rag_enterprise.domain.corpus import CorpusConfig
from agentic_rag_enterprise.domain.security import SecurityContext
from agentic_rag_enterprise.retrieval.hybrid import _HybridSearchAdapter
from agentic_rag_enterprise.retrieval.parent_reader import ParentReader
from agentic_rag_enterprise.retrieval.retriever import SecureRetriever
from agentic_rag_enterprise.security.policy import (
    AuthorizationDecision,
    ResourceAcl,
    evaluate_access,
)
from agentic_rag_enterprise.storage.vector_store import VectorStore
from tests.fixtures import (
    FakeDenseEncoder,
    FakeSparseEncoder,
    acl_payload,
    active_metadata_store,
    make_child_point,
    make_security_context,
)

CORPUS_ID = "engineering_wiki"
TENANT_ID = "t1"
DENSE_SIZE = 4


def _ctx(**overrides: object) -> SecurityContext:
    base: dict = dict(
        tenant_id=TENANT_ID,
        user_id="u1",
        groups=["g1"],
        allowed_security_levels=["public", "internal"],
        allowed_corpus_ids=None,
        is_admin=False,
    )
    base.update(overrides)
    return make_security_context(**base)


def _corpus() -> CorpusConfig:
    return CorpusConfig(
        corpus_id=CORPUS_ID,
        tenant_id=TENANT_ID,
        name="Eng",
        description="",
        domain="",
        owner="",
        source_type="wiki",
        capability_ids=[],
        enabled=True,
        searchable=True,
        security_policy_id="p",
        default_security_level="internal",
        created_at=datetime.now(),
        updated_at=datetime.now(),
    )


# (point_id, payload overrides). Defaults: active, non-deprecated, tenant t1.
_RESOURCES = [
    (1, {"acl_scope": "tenant", "security_level": "public"}),
    (2, {"acl_scope": "tenant", "security_level": "confidential"}),
    (3, {"acl_scope": "restricted", "security_level": "public"}),
    (4, {"acl_scope": "restricted", "security_level": "public", "allowed_user_ids": ["u1"]}),
    (5, {"acl_scope": "restricted", "security_level": "public", "allowed_group_ids": ["g1"]}),
    (
        6,
        {
            "acl_scope": "restricted",
            "security_level": "public",
            "allowed_user_ids": ["u1"],
            "denied_user_ids": ["u1"],
        },
    ),
    (
        7,
        {
            "acl_scope": "restricted",
            "security_level": "public",
            "allowed_group_ids": ["g1"],
            "denied_group_ids": ["g1"],
        },
    ),
    (8, {"tenant_id": "t2", "acl_scope": "tenant", "security_level": "public"}),
    (9, {"acl_scope": "restricted", "security_level": "confidential", "allowed_user_ids": ["u1"]}),
    (10, {"acl_scope": "tenant", "security_level": "public", "status": "deleted"}),
    (11, {"acl_scope": "tenant", "security_level": "public", "deprecated": True}),
    (12, {"acl_scope": "tenant", "security_level": "public", "corpus_id": "other_corpus"}),
]


def _build_store(resources: list | None = None) -> VectorStore:
    client = QdrantClient(location=":memory:")
    store = VectorStore(client)
    store.create_collection(CORPUS_ID, dense_size=DENSE_SIZE)
    points = []
    for pid, overrides in resources or _RESOURCES:
        base = {
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
        base.update(overrides)
        acl = acl_payload(
            tenant_id=base["tenant_id"],
            security_level=base["security_level"],
            acl_scope=base["acl_scope"],
            allowed_user_ids=base["allowed_user_ids"],
            allowed_group_ids=base["allowed_group_ids"],
            denied_user_ids=base["denied_user_ids"],
            denied_group_ids=base["denied_group_ids"],
        )
        points.append(
            make_child_point(
                pid,
                f"resource {pid} content text",
                tenant_id=base["tenant_id"],
                corpus_id=base["corpus_id"],
                document_id="d1",
                document_version="v1",
                parent_id=f"p{pid:012d}",
                acl=acl,
                status=base["status"],
                deprecated=base["deprecated"],
            )
        )
    store.upsert(CORPUS_ID, points)
    return store


def _expected(ctx: SecurityContext) -> set[int]:
    allowed: set[int] = set()
    for pid, overrides in _RESOURCES:
        base = {
            "tenant_id": TENANT_ID,
            "corpus_id": CORPUS_ID,
            "status": "active",
            "deprecated": False,
            "acl_scope": "restricted",
            "security_level": "public",
        }
        base.update(overrides)
        if (
            base["tenant_id"] != ctx.tenant_id
            or base["corpus_id"] != CORPUS_ID
            or base["status"] != "active"
            or base["deprecated"]
        ):
            continue
        acl = ResourceAcl(
            tenant_id=base["tenant_id"],
            security_level=base["security_level"],
            acl_scope=base["acl_scope"],
            allowed_user_ids=base.get("allowed_user_ids", []),
            allowed_group_ids=base.get("allowed_group_ids", []),
            denied_user_ids=base.get("denied_user_ids", []),
            denied_group_ids=base.get("denied_group_ids", []),
        )
        if evaluate_access(ctx, acl) is AuthorizationDecision.ALLOW:
            allowed.add(pid)
    return allowed


def _actual_pids(ctx: SecurityContext, store: VectorStore | None = None) -> set[int]:
    store = store or _build_store()
    retriever = _HybridSearchAdapter(store)
    hits = retriever.search(
        ctx,
        _corpus(),
        "resource content",
        top_k=100,
        dense_encoder=FakeDenseEncoder(),
        sparse_encoder=FakeSparseEncoder(),
    )
    return {int(h.parent_id.lstrip("p")) for h in hits}


def test_hybrid_matches_pdp() -> None:
    ctx = _ctx()
    assert _actual_pids(ctx) == _expected(ctx)


def test_security_level_denied() -> None:
    ids = _actual_pids(_ctx())
    assert 2 not in ids
    assert 9 not in ids


def test_restricted_empty_denied() -> None:
    ids = _actual_pids(_ctx())
    assert 3 not in ids


def test_user_and_group_allowed() -> None:
    ids = _actual_pids(_ctx())
    assert 4 in ids and 5 in ids


def test_deny_precedence() -> None:
    ids = _actual_pids(_ctx())
    assert 6 not in ids and 7 not in ids


def test_cross_tenant_denied() -> None:
    ids = _actual_pids(_ctx())
    assert 8 not in ids


def test_inactive_and_deprecated_denied() -> None:
    ids = _actual_pids(_ctx())
    assert 10 not in ids and 11 not in ids


def test_other_corpus_denied() -> None:
    ids = _actual_pids(_ctx())
    assert 12 not in ids


def test_empty_groups_still_returns_tenant_scope() -> None:
    ids = _actual_pids(_ctx(groups=[]))
    assert 1 in ids  # tenant scope, no groups needed
    assert 5 not in ids  # restricted group-only without groups


def test_empty_allowed_security_levels_fails_closed() -> None:
    # Empty levels => PEP raises EmptyAuthorizationScopeError; SecureRetriever
    # catches it and returns empty results (fail closed). The PDP also denies
    # everything, so PDP/PEP equivalence is preserved.
    retriever = SecureRetriever(
        _HybridSearchAdapter(_build_store()),
        ParentReader(_NoopStore()),
        metadata_store=active_metadata_store("t1", CORPUS_ID, "d1", "v1"),
    )
    result = retriever.retrieve(
        _ctx(allowed_security_levels=[]),
        "resource",
        _corpus(),
        100,
        dense_encoder=FakeDenseEncoder(),
        sparse_encoder=FakeSparseEncoder(),
    )
    assert result.hits == []


def test_empty_groups_cannot_match_reserved_payload() -> None:
    # A resource whose only allow path is a reserved sentinel group id must
    # still be denied when the caller has no groups (PEP omits the group
    # branch entirely, matching the PDP's empty-group semantics).
    reserved = "\x00__no_match__\x00"
    store = _build_store(
        [
            (
                1,
                {
                    "acl_scope": "restricted",
                    "security_level": "public",
                    "allowed_group_ids": [reserved],
                },
            )
        ]
    )
    ids = _actual_pids(_ctx(groups=[]), store)
    assert 1 not in ids  # sentinel group id never broadens access


def test_empty_levels_cannot_match_reserved_security_level() -> None:
    # With empty allowed levels the PEP raises; a crafted resource carrying the
    # reserved security level must not become readable.
    reserved = "\x00__no_match__\x00"
    retriever = SecureRetriever(
        _HybridSearchAdapter(
            _build_store(
                [
                    (
                        1,
                        {
                            "acl_scope": "tenant",
                            "security_level": reserved,
                        },
                    )
                ]
            )
        ),
        ParentReader(_NoopStore()),
        metadata_store=active_metadata_store("t1", CORPUS_ID, "d1", "v1"),
    )
    result = retriever.retrieve(
        _ctx(allowed_security_levels=[]),
        "resource",
        _corpus(),
        100,
        dense_encoder=FakeDenseEncoder(),
        sparse_encoder=FakeSparseEncoder(),
    )
    assert result.hits == []


def test_corpus_discoverability_gate_blocks() -> None:
    ctx = _ctx(allowed_corpus_ids=["other_corpus"])
    retriever = SecureRetriever(
        _HybridSearchAdapter(_build_store()),
        ParentReader(_NoopStore()),
        metadata_store=active_metadata_store("t1", CORPUS_ID, "d1", "v1"),
    )
    result = retriever.retrieve(
        ctx,
        "resource",
        _corpus(),
        dense_encoder=FakeDenseEncoder(),
        sparse_encoder=FakeSparseEncoder(),
    )
    assert result.hits == []


class _NoopStore:
    def get(self, parent_id):  # pragma: no cover - gate prevents reaching here
        return None
