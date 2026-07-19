"""E-021 unit tests for the deterministic temporal parser + filter.

Hermetic: no Qdrant, no LLM. Covers the frozen parser precedence (issue #1:
``as_of`` must not be mis-read as a ``range``) and the three filter modes.
"""

from datetime import datetime

import pytest

from agentic_rag_enterprise.domain.evidence import Evidence as SnapshotEvidence
from agentic_rag_enterprise.domain.temporal import (
    parse_temporal_scope,
)
from agentic_rag_enterprise.evidence.temporal import (
    filter_by_temporal_scope,
)


def _ev(
    *, evidence_id="e1", deprecated: bool = False, effective_from=None, effective_to=None
) -> SnapshotEvidence:
    return SnapshotEvidence(
        evidence_id=evidence_id,
        tenant_id="t1",
        corpus_id="c1",
        document_id="d1",
        document_version="v1",
        source_uri="u",
        source_filename="f",
        text="x",
        text_hash="h",
        retrieval_query="q",
        authority_level=50,
        effective_from=effective_from,
        effective_to=effective_to,
        deprecated=deprecated,
        retrieved_at=datetime(2024, 1, 1),
        acl_policy_id="p",
        policy_version="1",
        retrieval_iteration=0,
    )


# --- parser: mode classification (issue #1 precedence) -----------------------


def test_current_markers() -> None:
    for q in ("当前 API 版本是多少？", "current version", "now", "today", "现在的情况"):
        assert parse_temporal_scope(q).mode == "current"


def test_as_of_prefix_modes_and_date() -> None:
    for q, expected in (
        ("截至 2025-12-31 的配置", datetime(2025, 12, 31)),
        ("as of 2025-12-31", datetime(2025, 12, 31)),
        ("as_of 2025-12-31", datetime(2025, 12, 31)),
        ("截止 2025-12-31", datetime(2025, 12, 31)),
    ):
        scope = parse_temporal_scope(q)
        assert scope.mode == "as_of"
        assert scope.as_of == expected


def test_as_of_suffix_marker() -> None:
    scope = parse_temporal_scope("2025-12-31 为止的配置")
    assert scope.mode == "as_of"
    assert scope.as_of == datetime(2025, 12, 31)


def test_as_of_suppresses_bare_year_range() -> None:
    # "截至 2025-12-31" must be as_of, NEVER a 2025 year range (issue #1).
    scope = parse_temporal_scope("截至 2025-12-31 的年度报告")
    assert scope.mode == "as_of"
    assert scope.as_of == datetime(2025, 12, 31)
    assert scope.start is None and scope.end is None


def test_explicit_range_modes() -> None:
    for q in (
        "between 2024-01-01 and 2024-12-31",
        "from 2024-01-01 to 2024-12-31",
        "2024-01-01 至 2024-12-31",
        "2024-01-01 到 2024-12-31",
        "2024-01-01 ~ 2024-12-31",
    ):
        scope = parse_temporal_scope(q)
        assert scope.mode == "range", q
        assert scope.start == datetime(2024, 1, 1)
        assert scope.end == datetime(2024, 12, 31, 23, 59, 59)


def test_bare_year_range() -> None:
    scope = parse_temporal_scope("2024 年发生了什么")
    assert scope.mode == "range"
    assert scope.start == datetime(2024, 1, 1)
    assert scope.end == datetime(2024, 12, 31, 23, 59, 59)


def test_bare_date_range_single_day() -> None:
    scope = parse_temporal_scope("请查看 2025-06-15 的发布说明")
    assert scope.mode == "range"
    assert scope.start == datetime(2025, 6, 15, 0, 0, 0)
    assert scope.end == datetime(2025, 6, 15, 23, 59, 59)


def test_port_number_not_treated_as_year() -> None:
    # "8080" is not a year → unspecified (defensive guard).
    assert parse_temporal_scope("配置端口 8080").mode == "unspecified"


def test_unspecified_when_no_marker() -> None:
    assert parse_temporal_scope("如何配置超时").mode == "unspecified"


def test_parse_is_frozen_model() -> None:
    scope = parse_temporal_scope("当前")
    with pytest.raises(Exception):
        scope.mode = "range"  # type: ignore[misc]


# --- filter: current / unspecified -------------------------------------------


def test_current_drops_deprecated() -> None:
    now = datetime(2025, 1, 1)
    result = filter_by_temporal_scope(
        (_ev(deprecated=True), _ev()), parse_temporal_scope("current"), now=now
    )
    assert [e.evidence_id for e in result.retained] == ["e1"]
    assert result.filtered_out[0].reason == "deprecated"


def test_current_drops_expired() -> None:
    now = datetime(2025, 1, 1)
    expired = _ev(effective_to=datetime(2024, 1, 1))
    result = filter_by_temporal_scope((expired,), parse_temporal_scope("current"), now=now)
    assert result.retained == ()
    assert result.filtered_out[0].reason == "expired"


def test_current_drops_not_yet_effective() -> None:
    now = datetime(2025, 1, 1)
    future = _ev(effective_from=datetime(2026, 1, 1))
    result = filter_by_temporal_scope((future,), parse_temporal_scope("current"), now=now)
    assert result.retained == ()
    assert result.filtered_out[0].reason == "not_yet_effective"


def test_current_keeps_open_ended_non_deprecated() -> None:
    now = datetime(2025, 1, 1)
    result = filter_by_temporal_scope((_ev(),), parse_temporal_scope("current"), now=now)
    assert [e.evidence_id for e in result.retained] == ["e1"]


# --- filter: as_of (deprecated ignored) --------------------------------------


def test_as_of_keeps_effective_at_date() -> None:
    old = _ev(
        evidence_id="old", effective_from=datetime(2020, 1, 1), effective_to=datetime(2024, 6, 1)
    )
    new = _ev(evidence_id="new", effective_from=datetime(2024, 6, 2), effective_to=None)
    scope = parse_temporal_scope("截至 2024-01-01 的版本")
    result = filter_by_temporal_scope((old, new), scope)
    assert [e.evidence_id for e in result.retained] == ["old"]


def test_as_of_later_date_keeps_newer_version() -> None:
    old = _ev(
        evidence_id="old", effective_from=datetime(2020, 1, 1), effective_to=datetime(2024, 6, 1)
    )
    new = _ev(evidence_id="new", effective_from=datetime(2024, 6, 2), effective_to=None)
    scope = parse_temporal_scope("截至 2025-01-01 的版本")
    result = filter_by_temporal_scope((old, new), scope)
    assert [e.evidence_id for e in result.retained] == ["new"]


def test_as_of_ignores_deprecated_flag() -> None:
    dep = _ev(deprecated=True, effective_from=datetime(2020, 1, 1), effective_to=None)
    scope = parse_temporal_scope("截至 2025-01-01 的版本")
    result = filter_by_temporal_scope((dep,), scope)
    assert [e.evidence_id for e in result.retained] == ["e1"]


# --- filter: range (overlap) -------------------------------------------------


def test_range_overlap_keeps_overlapping() -> None:
    ev = _ev(effective_from=datetime(2024, 1, 1), effective_to=datetime(2024, 12, 31))
    scope = parse_temporal_scope("between 2024-06-01 and 2024-07-01")
    result = filter_by_temporal_scope((ev,), scope)
    assert result.retained == (ev,)


def test_range_non_overlap_filters_out() -> None:
    ev = _ev(
        evidence_id="old", effective_from=datetime(2020, 1, 1), effective_to=datetime(2022, 1, 1)
    )
    scope = parse_temporal_scope("between 2024-01-01 and 2024-12-31")
    result = filter_by_temporal_scope((ev,), scope)
    assert result.retained == ()
    assert result.filtered_out[0].reason == "out_of_window"


def test_filter_preserves_input_order() -> None:
    evs = (_ev(evidence_id="a"), _ev(evidence_id="b"), _ev(evidence_id="c", deprecated=True))
    result = filter_by_temporal_scope(
        evs, parse_temporal_scope("current"), now=datetime(2025, 1, 1)
    )
    assert [e.evidence_id for e in result.retained] == ["a", "b"]


def test_filter_returns_frozen_result() -> None:
    from agentic_rag_enterprise.evidence.temporal import TemporalFilterResult

    result = filter_by_temporal_scope(
        (_ev(),), parse_temporal_scope("current"), now=datetime(2025, 1, 1)
    )
    assert isinstance(result, TemporalFilterResult)
    assert isinstance(result.retained, tuple)
    with pytest.raises(Exception):
        result.retained = ()  # type: ignore[misc]
