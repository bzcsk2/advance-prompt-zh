"""Unit tests for the E-016 permission-aware soft router (corpus/router.py).

These lock the *query-sensitive* soft-routing semantics required by build plan
§9.3: normalized [0,1] scores, ``route_confidence`` / ``fallback_search``, and
Top-1/2/3 selection driven by query relevance (not raw authority).
"""

from agentic_rag_enterprise.corpus.registry import InMemoryCorpusRegistry
from agentic_rag_enterprise.corpus.router import CorpusRouter
from agentic_rag_enterprise.domain.security import SecurityContext


def _ctx(tenant_id: str = "local", allowed_corpus_ids: list[str] | None = None) -> SecurityContext:
    return SecurityContext(
        request_id="r",
        session_id="s",
        tenant_id=tenant_id,
        user_id="u",
        policy_version="1.0",
        allowed_corpus_ids=allowed_corpus_ids,
    )


def test_scores_are_normalized_to_unit_interval() -> None:
    router = CorpusRouter()
    route = router.route(
        "how do I configure the product feature?", _ctx(), InMemoryCorpusRegistry()
    )
    assert route.candidates
    for c in route.candidates:
        assert 0.0 <= c.score <= 1.0
        assert 0.0 <= c.relevance <= 1.0


def test_query_relevance_surfaces_tickets_corpus() -> None:
    # A ticket-oriented query must be able to route to `tickets` even though it has
    # the LOWEST authority (40). Pure-authority routing would never pick it.
    router = CorpusRouter()
    route = router.route(
        "how to handle incident tickets and support failures?",
        _ctx(),
        InMemoryCorpusRegistry(),
    )
    assert "tickets" in [c.corpus_id for c in route.candidates]


def test_query_relevance_beats_authority_for_top1() -> None:
    router = CorpusRouter()
    route = router.route(
        "support ticket incident triage",
        _ctx(),
        InMemoryCorpusRegistry(),
    )
    # The most relevant corpus ranks first regardless of its lower authority.
    assert route.candidates[0].corpus_id == "tickets"


def test_high_confidence_routes_top1() -> None:
    # A query dominated by one corpus' unique terms → high confidence, Top-1.
    router = CorpusRouter()
    route = router.route(
        "troubleshooting workarounds resolution notes",
        _ctx(),
        InMemoryCorpusRegistry(),
    )
    assert route.route_confidence == "high"
    assert len(route.candidates) == 1
    assert route.candidates[0].corpus_id == "tickets"
    assert route.fallback_search is False


def test_low_confidence_query_broadens_and_flags_fallback() -> None:
    # A query with no matching corpus term must NOT hard-route to authority; it
    # broadens (Top-3) and flags fallback_search (§9.3).
    router = CorpusRouter()
    route = router.route("zzzz qqqq wxyz", _ctx(), InMemoryCorpusRegistry())
    assert route.route_confidence == "low"
    assert route.fallback_search is True
    assert len(route.candidates) == 3


def test_medium_confidence_routes_top2() -> None:
    # A moderately relevant query with no dominant winner → Top-2, no fallback.
    router = CorpusRouter()
    route = router.route(
        "product documentation and engineering notes", _ctx(), InMemoryCorpusRegistry()
    )
    assert route.route_confidence == "medium"
    assert len(route.candidates) == 2
    assert route.fallback_search is False


def test_route_excludes_undiscoverable_corpus_and_rationale_non_leaky() -> None:
    router = CorpusRouter()
    ctx = _ctx(allowed_corpus_ids=["product_docs"])
    route = router.route("compare tickets and wiki", ctx, InMemoryCorpusRegistry())
    # Only the allowed corpus is ever considered; the others never appear, and the
    # rationale does not leak their existence.
    assert [c.corpus_id for c in route.candidates] == ["product_docs"]
    for c in route.candidates:
        assert "tickets" not in c.rationale
        assert "engineering" not in c.rationale


def test_explicit_limit_truncates_policy_count() -> None:
    router = CorpusRouter()
    route = router.route("zzzz qqqq wxyz", _ctx(), InMemoryCorpusRegistry(), limit=1)
    # low confidence would pick 3, but an explicit limit truncates (never widens).
    assert len(route.candidates) == 1


def test_route_limit_must_be_positive() -> None:
    router = CorpusRouter()
    try:
        router.route("q", _ctx(), InMemoryCorpusRegistry(), limit=0)
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_route_deterministic() -> None:
    router = CorpusRouter()
    reg = InMemoryCorpusRegistry()
    q = "support ticket incident triage"
    a = router.route(q, _ctx(), reg)
    b = router.route(q, _ctx(), reg)
    assert [(c.corpus_id, c.score) for c in a.candidates] == [
        (c.corpus_id, c.score) for c in b.candidates
    ]
    assert a.route_confidence == b.route_confidence
