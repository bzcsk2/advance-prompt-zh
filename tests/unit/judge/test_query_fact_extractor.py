"""Unit tests for E-019 DeterministicQueryFactExtractor (build plan §7.7 / §14.1)."""

from agentic_rag_enterprise.judge.query_fact_extractor import (
    DeterministicQueryFactExtractor,
    make_required_fact,
)


def test_supplied_overrides_decomposition() -> None:
    supplied = [make_required_fact("alpha requirement"), make_required_fact("beta requirement")]
    out = DeterministicQueryFactExtractor().extract("ignored query", supplied=supplied)
    assert out == supplied


def test_empty_supplied_returns_empty() -> None:
    out = DeterministicQueryFactExtractor().extract("anything", supplied=[])
    assert out == []


def test_clause_split_on_punctuation() -> None:
    out = DeterministicQueryFactExtractor().extract(
        "what is the vacation policy? how do i request time off?"
    )
    descs = {f.description for f in out}
    assert "what is the vacation policy" in descs
    assert "how do i request time off" in descs


def test_clause_split_on_and() -> None:
    out = DeterministicQueryFactExtractor().extract(
        "what is the vacation policy and the bonus structure"
    )
    descs = {f.description for f in out}
    assert "what is the vacation policy" in descs
    assert "the bonus structure" in descs


def test_clause_split_on_or() -> None:
    out = DeterministicQueryFactExtractor().extract("compare the alpha plan or the beta plan")
    descs = {f.description for f in out}
    assert "compare the alpha plan" in descs
    assert "the beta plan" in descs


def test_make_required_fact_normalizes_and_is_stable() -> None:
    a = make_required_fact("Vacation Policy!")
    b = make_required_fact("vacation policy")
    assert a.fact_id == b.fact_id
    assert a.description == "vacation policy"
    assert a.required is True


def test_extracted_fact_ids_are_stable() -> None:
    facts = DeterministicQueryFactExtractor().extract("what is the vacation policy?")
    assert len(facts) == 1
    assert facts[0].fact_id == make_required_fact("what is the vacation policy").fact_id
