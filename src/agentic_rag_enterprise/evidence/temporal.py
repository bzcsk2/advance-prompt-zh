"""E-021 temporal filter over an already-authorized Evidence collection.

Runs *after* retrieval (post authorization / active-version gate), *before* the
conflict resolver. Pure and order-preserving: it never re-orders, mutates, or
adds evidence (build plan §15.4).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict

from agentic_rag_enterprise.domain.evidence import Evidence as SnapshotEvidence
from agentic_rag_enterprise.domain.temporal import TemporalScope


def _naive_utc(dt: datetime) -> datetime:
    """Normalize a (possibly tz-aware) datetime to a naive UTC datetime.

    The Evidence model carries naive datetimes, but callers in tests/ingestion
    sometimes supply tz-aware ones. Comparisons must not mix the two (``TypeError:
    can't compare offset-naive and offset-aware datetimes``), so we normalize to
    naive UTC at every comparison boundary.
    """
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


FilterReason = Literal["deprecated", "expired", "not_yet_effective", "out_of_window"]


class FilteredEvidence(BaseModel):
    """An evidence snapshot dropped by the temporal filter, with the reason."""

    model_config = ConfigDict(frozen=True)

    evidence: SnapshotEvidence
    reason: FilterReason


class TemporalFilterResult(BaseModel):
    """The retained + filtered-out partition for one scope."""

    model_config = ConfigDict(frozen=True)

    scope: TemporalScope
    retained: tuple[SnapshotEvidence, ...]
    filtered_out: tuple[FilteredEvidence, ...]


def filter_by_temporal_scope(
    evidence: tuple[SnapshotEvidence, ...] | list[SnapshotEvidence],
    scope: TemporalScope,
    *,
    now: datetime | None = None,
) -> TemporalFilterResult:
    """Partition ``evidence`` by ``scope``.

    Rules (build plan §15.4):

    * ``current`` / ``unspecified`` (target = ``now``): drop ``deprecated``,
      drop ``effective_to`` set and past (expired), drop ``effective_from`` set
      and future (not yet effective).
    * ``as_of`` (target = ``scope.as_of``): keep iff the effective window covers
      the date. ``deprecated`` is **ignored** (it reflects *current* state).
    * ``range`` (window = ``[start, end]``): keep iff the effective window
      *overlaps* the scope window. ``deprecated`` is **ignored**.

    Retained evidence follows the input order.
    """
    now = now or datetime.now()
    evs = list(evidence)

    retained: list[SnapshotEvidence] = []
    filtered_out: list[FilteredEvidence] = []

    if scope.mode in ("current", "unspecified"):
        target = _naive_utc(now)
        for ev in evs:
            if ev.deprecated:
                filtered_out.append(FilteredEvidence(evidence=ev, reason="deprecated"))
            elif ev.effective_to is not None and _naive_utc(ev.effective_to) < target:
                filtered_out.append(FilteredEvidence(evidence=ev, reason="expired"))
            elif ev.effective_from is not None and _naive_utc(ev.effective_from) > target:
                filtered_out.append(FilteredEvidence(evidence=ev, reason="not_yet_effective"))
            else:
                retained.append(ev)
    elif scope.mode == "as_of":
        as_of_target: datetime | None = _naive_utc(scope.as_of) if scope.as_of is not None else None
        if as_of_target is None:
            # No usable date — cannot judge; retain everything (defensive).
            retained = list(evs)
        else:
            for ev in evs:
                ef = _naive_utc(ev.effective_from) if ev.effective_from is not None else None
                et = _naive_utc(ev.effective_to) if ev.effective_to is not None else None
                ok_from = ef is None or ef <= as_of_target
                ok_to = et is None or et >= as_of_target
                if ok_from and ok_to:
                    retained.append(ev)
                else:
                    filtered_out.append(FilteredEvidence(evidence=ev, reason="out_of_window"))
    elif scope.mode == "range":
        start, end = scope.start, scope.end
        if start is not None:
            start = _naive_utc(start)
        if end is not None:
            end = _naive_utc(end)
        for ev in evs:
            if start is None or end is None:
                retained.append(ev)
                continue
            ef = _naive_utc(ev.effective_from) if ev.effective_from is not None else None
            et = _naive_utc(ev.effective_to) if ev.effective_to is not None else None
            ok_from = ef is None or ef <= end
            ok_to = et is None or et >= start
            if ok_from and ok_to:
                retained.append(ev)
            else:
                filtered_out.append(FilteredEvidence(evidence=ev, reason="out_of_window"))
    else:  # pragma: no cover - Literal guarantees a known mode
        retained = list(evs)

    return TemporalFilterResult(
        scope=scope,
        retained=tuple(retained),
        filtered_out=tuple(filtered_out),
    )
