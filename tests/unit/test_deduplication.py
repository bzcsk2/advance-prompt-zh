"""Unit tests for the retrieval Deduplicator (build plan §12.6)."""

from agentic_rag_enterprise.retrieval.deduplication import (
    DedupCandidate,
    Deduplicator,
    RetrievalContext,
)
from agentic_rag_enterprise.retrieval.models import RetrievalHit


def _hit(
    *,
    chunk_id: str,
    parent_id: str,
    document_id: str = "doc1",
    document_version: str = "v1",
    corpus_id: str = "eng",
    tenant_id: str = "t1",
    text: str = "the system routes queries",
    score: float = 1.0,
) -> RetrievalHit:
    return RetrievalHit(
        chunk_id=chunk_id,
        parent_id=parent_id,
        document_id=document_id,
        document_version=document_version,
        corpus_id=corpus_id,
        tenant_id=tenant_id,
        text=text,
        score=score,
        section_path=[],
        status="active",
        deprecated=False,
        security_level="internal",
        acl_scope="tenant",
        allowed_user_ids=[],
        allowed_group_ids=[],
        denied_user_ids=[],
        denied_group_ids=[],
    )


def _cand(hit: RetrievalHit, *, authority_level: int = 50, query: str = "q") -> DedupCandidate:
    return DedupCandidate(
        hit=hit,
        contexts=[RetrievalContext(query=query)],
        text=hit.text,
        authority_level=authority_level,
    )


def test_exact_span_dedup_keeps_highest_score_and_merges_queries() -> None:
    # Two hits with the same (document, version, chunk) but different scores
    # and query contexts -> one survivor, highest score, merged contexts.
    low = _cand(_hit(chunk_id="c1", parent_id="p1", score=0.4), query="first")
    high = _cand(_hit(chunk_id="c1", parent_id="p1", score=0.9), query="second")
    survivors = Deduplicator().deduplicate([low, high])
    assert len(survivors) == 1
    assert survivors[0].hit.score == 0.9
    queries = {c.query for c in survivors[0].contexts}
    assert queries == {"first", "second"}


def test_same_parent_multi_child_collapses() -> None:
    # Two distinct children of the same parent -> one survivor (highest score).
    a = _cand(_hit(chunk_id="c1", parent_id="p1", score=0.7, text="alpha window one"))
    b = _cand(_hit(chunk_id="c2", parent_id="p1", score=0.3, text="alpha window two"))
    survivors = Deduplicator().deduplicate([a, b])
    assert len(survivors) == 1
    assert survivors[0].hit.chunk_id == "c1"  # higher score wins
    assert survivors[0].duplicate_sources  # the loser is recorded


def test_near_duplicate_text_collapses() -> None:
    a = _cand(_hit(chunk_id="c1", parent_id="p1", text="The system routes queries"))
    b = _cand(_hit(chunk_id="c2", parent_id="p2", text="the system routes queries"))
    survivors = Deduplicator().deduplicate([a, b])
    assert len(survivors) == 1
    assert len(survivors[0].duplicate_sources) == 1


def test_cross_corpus_keeps_higher_authority() -> None:
    # Same normalized text copied across two corpora; higher authority wins.
    low_auth = _cand(
        _hit(chunk_id="c1", parent_id="p1", corpus_id="tickets", text="service outage cause"),
        authority_level=40,
    )
    high_auth = _cand(
        _hit(chunk_id="c2", parent_id="p2", corpus_id="eng", text="Service outage cause"),
        authority_level=80,
    )
    survivors = Deduplicator().deduplicate([low_auth, high_auth])
    assert len(survivors) == 1
    assert survivors[0].hit.corpus_id == "eng"
    assert survivors[0].authority_level == 80
    recorded = {d["corpus_id"] for d in survivors[0].duplicate_sources}
    assert "tickets" in recorded


def test_cross_corpus_equal_authority_keeps_higher_score() -> None:
    a = _cand(
        _hit(chunk_id="c1", parent_id="p1", corpus_id="eng", text="duplicate body", score=0.2),
        authority_level=50,
    )
    b = _cand(
        _hit(chunk_id="c2", parent_id="p2", corpus_id="wiki", text="duplicate body", score=0.8),
        authority_level=50,
    )
    survivors = Deduplicator().deduplicate([a, b])
    assert len(survivors) == 1
    assert survivors[0].hit.score == 0.8


def test_distinct_hits_all_survive() -> None:
    a = _cand(_hit(chunk_id="c1", parent_id="p1", text="architecture overview"))
    b = _cand(_hit(chunk_id="c2", parent_id="p2", text="security model"))
    c = _cand(_hit(chunk_id="c3", parent_id="p3", text="planner selects corpora"))
    survivors = Deduplicator().deduplicate([a, b, c])
    assert len(survivors) == 3


def test_order_preserved_by_score_descending() -> None:
    low = _cand(_hit(chunk_id="c1", parent_id="p1", text="x", score=0.1))
    high = _cand(_hit(chunk_id="c2", parent_id="p2", text="y", score=0.9))
    mid = _cand(_hit(chunk_id="c3", parent_id="p3", text="z", score=0.5))
    survivors = Deduplicator().deduplicate([low, high, mid])
    scores = [s.hit.score for s in survivors]
    assert scores == [0.9, 0.5, 0.1]


def test_empty_input_returns_empty() -> None:
    assert Deduplicator().deduplicate([]) == []
