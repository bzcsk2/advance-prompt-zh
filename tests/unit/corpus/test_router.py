"""Unit tests for the E-016 permission-aware soft router (corpus/router.py)."""

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


def test_route_returns_only_discoverable_top_n() -> None:
    router = CorpusRouter()
    ctx = _ctx()  # unrestricted → all three fixtures discoverable
    route = router.route("how do I configure X?", ctx, InMemoryCorpusRegistry(), limit=2)
    # Sorted by descending authority_level: product_docs(80), engineering_wiki(70).
    assert [c.corpus_id for c in route.candidates] == ["product_docs", "engineering_wiki"]
    assert route.truncated_from == 3


def test_route_limit_1_returns_single_corpus() -> None:
    router = CorpusRouter()
    ctx = _ctx()
    route = router.route("question", ctx, InMemoryCorpusRegistry(), limit=1)
    assert [c.corpus_id for c in route.candidates] == ["product_docs"]


def test_route_excludes_undiscoverable_corpus() -> None:
    router = CorpusRouter()
    ctx = _ctx(allowed_corpus_ids=["product_docs"])
    route = router.route("compare", ctx, InMemoryCorpusRegistry(), limit=5)
    # Only the allowed corpus is ever considered; the others never appear, and
    # the rationale does not leak their existence.
    assert [c.corpus_id for c in route.candidates] == ["product_docs"]
    for c in route.candidates:
        assert "tickets" not in c.rationale
        assert "engineering" not in c.rationale


def test_route_score_is_authority_and_rationale_non_leaky() -> None:
    router = CorpusRouter()
    ctx = _ctx()
    route = router.route("q", ctx, InMemoryCorpusRegistry(), limit=3)
    for c in route.candidates:
        assert c.score == float(c.authority_level)
        assert c.rationale == f"authority={c.authority_level}"


def test_route_limit_must_be_positive() -> None:
    router = CorpusRouter()
    ctx = _ctx()
    try:
        router.route("q", ctx, InMemoryCorpusRegistry(), limit=0)
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_route_deterministic_order() -> None:
    router = CorpusRouter()
    ctx = _ctx()
    reg = InMemoryCorpusRegistry()
    a = router.route("q", ctx, reg, limit=3)
    b = router.route("q", ctx, reg, limit=3)
    assert [c.corpus_id for c in a.candidates] == [c.corpus_id for c in b.candidates]
