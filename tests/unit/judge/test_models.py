"""Unit tests for E-019 judge models (build plan §7.7 / §7.8 / §14.3)."""

from agentic_rag_enterprise.judge.models import (
    FactCoverage,
    FactStatus,
    build_sufficiency_result,
    derive_overall_status,
)


def _fc(fact_id: str, status: FactStatus, *, required: bool = True) -> FactCoverage:
    return FactCoverage(fact_id=fact_id, status=status, required=required)


def test_derive_overall_sufficient() -> None:
    assert derive_overall_status([_fc("a", FactStatus.SUPPORTED)]) == "sufficient"


def test_derive_overall_policy_blocked_priority() -> None:
    # policy_blocked wins over every other status (§14.3 fixed priority).
    assert (
        derive_overall_status([_fc("a", FactStatus.SUPPORTED), _fc("b", FactStatus.POLICY_BLOCKED)])
        == "policy_blocked"
    )


def test_derive_overall_ambiguous_priority() -> None:
    assert (
        derive_overall_status([_fc("a", FactStatus.SUPPORTED), _fc("b", FactStatus.AMBIGUOUS)])
        == "ambiguous"
    )


def test_derive_overall_contradicted_priority() -> None:
    assert (
        derive_overall_status([_fc("a", FactStatus.SUPPORTED), _fc("b", FactStatus.CONTRADICTED)])
        == "contradicted"
    )


def test_derive_overall_partially_when_some_supported() -> None:
    assert (
        derive_overall_status([_fc("a", FactStatus.SUPPORTED), _fc("b", FactStatus.MISSING)])
        == "partially_sufficient"
    )


def test_derive_overall_insufficient_when_none_supported() -> None:
    assert (
        derive_overall_status(
            [_fc("a", FactStatus.MISSING), _fc("b", FactStatus.PARTIALLY_SUPPORTED)]
        )
        == "insufficient"
    )


def test_derive_overall_empty_is_sufficient() -> None:
    # Vacuously satisfied when there are no required facts.
    assert derive_overall_status([]) == "sufficient"


def test_build_sufficiency_result_aggregates() -> None:
    coverages = [_fc("a", FactStatus.SUPPORTED), _fc("b", FactStatus.MISSING)]
    res = build_sufficiency_result(coverages=coverages, required_fact_ids={"a", "b"})
    assert res.overall_status == "partially_sufficient"
    assert res.covered_fact_ids == ("a",)
    assert res.missing_fact_ids == ("b",)
    assert res.can_continue_retrieval is True
    assert res.should_abstain is False


def test_build_sufficiency_result_optional_fact_does_not_force_insufficient() -> None:
    # An optional (required=False) missing fact must not force an insufficient verdict.
    coverages = [
        _fc("a", FactStatus.SUPPORTED, required=True),
        _fc("b", FactStatus.MISSING, required=False),
    ]
    res = build_sufficiency_result(coverages=coverages, required_fact_ids={"a"})
    assert res.overall_status == "sufficient"
    # The optional missing fact is still reported in missing_fact_ids, but it did
    # not flip the overall verdict to insufficient.
    assert "b" in res.missing_fact_ids
