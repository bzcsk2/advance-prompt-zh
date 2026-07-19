"""E-021 unit tests for the ConflictResolver + structured-assertion parser.

Hermetic. Covers the four resolution rules, the five MVP acceptance scenarios,
the two knowledge-pollution cases, and the cross-cutting invariants (8–10).
"""

import sys
import types
from datetime import datetime

from agentic_rag_enterprise.domain.evidence import Evidence as SnapshotEvidence
from agentic_rag_enterprise.domain.temporal import parse_temporal_scope
from agentic_rag_enterprise.evidence.conflict_resolver import (
    AUTHORITY_OVERRIDE_MARGIN,
    ConflictResolver,
)
from agentic_rag_enterprise.evidence.models import (
    ConflictStatus,
    ConflictType,
    ConflictResolution,
    extract_assertion,
    normalize_topic_key,
)


def _ev(eid, *, doc="d1", ver="v1", auth=50, text="x", **kw) -> SnapshotEvidence:
    base = dict(
        tenant_id="t1",
        corpus_id="c1",
        document_id=doc,
        document_version=ver,
        source_uri="u",
        source_filename="f",
        text=text,
        text_hash="h",
        retrieval_query="q",
        authority_level=auth,
        retrieved_at=datetime(2024, 1, 1),
        acl_policy_id="p",
        policy_version="1",
        retrieval_iteration=0,
    )
    base.update(kw)
    return SnapshotEvidence(evidence_id=eid, **base)


def _resolve(*evs, query="当前 API 版本") -> object:
    scope = parse_temporal_scope(query)
    return ConflictResolver().resolve(evs, scope, topic_key=normalize_topic_key(query))


# --- structured-assertion parser (issue #3) ----------------------------------


def test_extract_version() -> None:
    a = extract_assertion("version: 2")
    assert a.is_structured and a.value_kind == "version" and a.value == "v2"


def test_extract_key_value() -> None:
    a = extract_assertion("timeout: 30s")
    assert a.is_structured and a.value_kind == "key_value"
    assert a.key == "timeout" and a.value == "30s"


def test_extract_quantity() -> None:
    a = extract_assertion("limit 60s")
    assert a.is_structured and a.value_kind == "quantity" and a.value == "60s"


def test_extract_boolean() -> None:
    assert extract_assertion("开启").value == "开启"
    assert extract_assertion("true").value == "true"


def test_extract_unstructured_is_pass_through() -> None:
    assert extract_assertion("some free-text migration note").is_structured is False


def test_normalize_topic_key() -> None:
    assert normalize_topic_key("当前 API 版本?") == "当前api版本"


# --- acceptance scenarios ----------------------------------------------------


def test_acceptance_1_current_new_version_supersedes_old() -> None:
    rep = _resolve(
        _ev("e1", doc="d1", ver="v1", text="version v1"),
        _ev("e2", doc="d1", ver="v2", text="version v2"),
    )
    assert rep.conflict_status == ConflictStatus.RESOLVED
    assert rep.resolved_evidence_ids == ("e2",)
    f = rep.findings[0]
    assert f.conflict_type == ConflictType.VERSION_CONFLICT
    assert f.resolution == ConflictResolution.AUTO_VERSION


def test_acceptance_3_authority_conflict_keeps_higher() -> None:
    prod = _ev("p", doc="prod", ver="v1", auth=80, text="version: 2")
    ticket = _ev("t", doc="ticket", ver="v1", auth=40, text="version: 3")
    rep = _resolve(prod, ticket)
    assert rep.conflict_status == ConflictStatus.RESOLVED
    assert rep.resolved_evidence_ids == ("p",)
    assert rep.findings[0].resolution == ConflictResolution.AUTO_AUTHORITY


def test_acceptance_4_temporary_rollback_escalates() -> None:
    adr = _ev(
        "adr",
        doc="adr1",
        ver="v2",
        auth=80,
        text="version: 2",
        effective_from=None,
        effective_to=None,
    )
    ticket = _ev(
        "tk",
        doc="tk1",
        ver="v1",
        auth=40,
        text="version: 1",
        effective_from=datetime(2025, 1, 1),
        effective_to=datetime(2025, 12, 31),
    )
    rep = _resolve(adr, ticket)
    assert rep.conflict_status == ConflictStatus.CONTRADICTED
    assert rep.findings[0].conflict_type == ConflictType.TIME_CONFLICT
    assert rep.findings[0].resolvable is False
    # Both sources must be preserved (not silently picking v2).
    assert set(rep.resolved_evidence_ids) == {"adr", "tk"}


def test_acceptance_5_unresolvable_equal_authority() -> None:
    a = _ev("a", doc="da", ver="v1", auth=50, text="version: 2")
    b = _ev("b", doc="db", ver="v1", auth=50, text="version: 3")
    rep = _resolve(a, b)
    assert rep.conflict_status == ConflictStatus.CONTRADICTED
    assert rep.findings[0].resolution == ConflictResolution.UNRESOLVED
    assert set(rep.resolved_evidence_ids) == {"a", "b"}


def test_acceptance_2_as_of_historical_filter() -> None:
    # Resolver runs post-filter; here we assert the as_of filter keeps only the
    # in-force version and the resolver then sees a single (NONE) evidence.
    from agentic_rag_enterprise.evidence.temporal import filter_by_temporal_scope

    old = _ev(
        "old",
        doc="d1",
        ver="v1",
        text="version v1",
        effective_from=datetime(2020, 1, 1),
        effective_to=datetime(2024, 6, 1),
    )
    new = _ev("new", doc="d1", ver="v2", text="version v2", effective_from=datetime(2024, 6, 2))
    scope = parse_temporal_scope("截至 2024-01-01 的版本")
    retained = filter_by_temporal_scope((old, new), scope).retained
    rep = ConflictResolver().resolve(retained, scope, topic_key="x")
    assert rep.conflict_status == ConflictStatus.NONE
    assert rep.resolved_evidence_ids == ("old",)


# --- knowledge-pollution cases -----------------------------------------------


def test_pollution_low_vs_high_authority_resolves_to_high() -> None:
    ticket = _ev("tk", doc="tk1", ver="v1", auth=40, text="version: 1")
    prod = _ev("pd", doc="pd1", ver="v1", auth=80, text="version: 2")
    rep = _resolve(ticket, prod)
    assert rep.conflict_status == ConflictStatus.RESOLVED
    assert rep.resolved_evidence_ids == ("pd",)


def test_pollution_new_version_supersedes_old() -> None:
    rep = _resolve(
        _ev("old", doc="d1", ver="v1", text="version v1"),
        _ev("new", doc="d1", ver="v2", text="version v2"),
    )
    assert rep.conflict_status == ConflictStatus.RESOLVED
    assert rep.resolved_evidence_ids == ("new",)


# --- scope conflict (complementary, not contradictory) -----------------------


def test_scope_conflict_keeps_both() -> None:
    a = _ev("a", doc="d1", ver="v1", auth=50, text="timeout: 30s")
    b = _ev("b", doc="d1", ver="v1", auth=50, text="retries: 5")
    rep = _resolve(a, b)
    assert rep.conflict_status == ConflictStatus.RESOLVED
    f = rep.findings[0]
    assert f.conflict_type == ConflictType.SCOPE_CONFLICT
    assert f.resolution == ConflictResolution.AUTO_SCOPE
    assert set(rep.resolved_evidence_ids) == {"a", "b"}


# --- no-conflict regression (M2–M5) ------------------------------------------


def test_no_conflict_single_free_text_is_none() -> None:
    rep = _resolve(_ev("o", doc="d1", ver="v1", text="a free-text migration note"))
    assert rep.conflict_status == ConflictStatus.NONE
    assert rep.resolved_evidence_ids == ("o",)


def test_no_conflict_same_value_is_none() -> None:
    rep = _resolve(
        _ev("a", doc="da", ver="v1", auth=50, text="version: 2"),
        _ev("b", doc="db", ver="v1", auth=50, text="version: 2"),
    )
    assert rep.conflict_status == ConflictStatus.NONE


# --- invariants 8–10 ---------------------------------------------------------


def test_inv8_conflict_result_keeps_evidence_id_and_source() -> None:
    a = _ev(
        "a",
        doc="da",
        ver="v1",
        auth=50,
        text="version: 2",
        section_path=("sec1",),
        effective_from=datetime(2024, 1, 1),
        effective_to=datetime(2024, 12, 31),
    )
    b = _ev("b", doc="db", ver="v1", auth=50, text="version: 3")
    rep = _resolve(a, b)
    assert rep.conflict_status == ConflictStatus.CONTRADICTED
    src_ids = {s.evidence_id for f in rep.findings for s in f.sources}
    assert src_ids == {"a", "b"}
    for f in rep.findings:
        for s in f.sources:
            assert s.document_id
            assert s.document_version
            assert s.evidence_id


def test_inv9_no_vector_relevance_selection() -> None:
    # The loser has a HIGHER retrieval_score but LOWER authority. Authority (not
    # vector relevance) must decide; resolution never uses retrieval/rerank score.
    high_score_los = _ev(
        "low",
        doc="tk1",
        ver="v1",
        auth=40,
        text="version: 1",
        retrieval_score=0.99,
        rerank_score=0.99,
    )
    low_score_win = _ev(
        "high",
        doc="pd1",
        ver="v1",
        auth=80,
        text="version: 2",
        retrieval_score=0.10,
        rerank_score=0.10,
    )
    rep = _resolve(high_score_los, low_score_win)
    assert rep.conflict_status == ConflictStatus.RESOLVED
    assert rep.resolved_evidence_ids == ("high",)  # chosen by authority, not score


def test_inv10_resolver_has_no_planner_dependency() -> None:
    mod = sys.modules["agentic_rag_enterprise.evidence.conflict_resolver"]
    for name in dir(mod):
        obj = getattr(mod, name)
        if isinstance(obj, types.ModuleType) and obj.__name__.startswith(
            "agentic_rag_enterprise.planner"
        ):
            raise AssertionError(f"ConflictResolver imports planner module: {obj.__name__}")


def test_authority_margin_configurable() -> None:
    # Margin default forbids a 5-point gap from auto-overriding.
    a = _ev("a", doc="da", ver="v1", auth=50, text="version: 2")
    b = _ev("b", doc="db", ver="v1", auth=55, text="version: 3")
    rep = _resolve(a, b)
    assert rep.conflict_status == ConflictStatus.CONTRADICTED  # 5 < AUTHORITY_OVERRIDE_MARGIN
    assert AUTHORITY_OVERRIDE_MARGIN == 20
