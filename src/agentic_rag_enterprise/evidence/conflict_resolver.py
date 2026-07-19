"""E-021 conflict resolver (build plan §15.3).

Deterministic, conservative, and Planner-free: it consumes *only* already-
authorized, post-temporal-filter ``Evidence`` and emits a resolver-only
``ConflictReport``. It never judges sufficiency (issue #2) and never selects a
"most likely" answer by vector relevance (invariant 3).

Candidate conflicts are created **only** under strict conditions (issue #3):
same-``document_id`` version divergence (``VERSION_CONFLICT``), or a structured
``assertion`` parser extracting a *different* value on the *same* key with
overlapping effective time (``VALUE_CONFLICT`` / ``SCOPE_CONFLICT``). Differing
free ``text`` alone never creates a candidate.

Four explicit resolution rules, in precedence:

1. **version** — newer ``document_version`` supersedes older (``AUTO_VERSION``).
2. **time** — a temporary rollback overlapping a permanent statement effective
   now escalates to ``CONTRADICTED`` rather than being silently superseded
   (temporary-rollback guard).
3. **authority** — a clearly higher ``authority_level`` overrides a lower one
   (``AUTO_AUTHORITY``, margin-gated).
4. **unresolved** — anything else escalates to ``CONTRADICTED``; no winner chosen.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from agentic_rag_enterprise.domain.evidence import Evidence as SnapshotEvidence
from agentic_rag_enterprise.domain.temporal import TemporalScope
from agentic_rag_enterprise.evidence.models import (
    ConflictFinding,
    ConflictReport,
    ConflictResolution,
    ConflictStatus,
    ConflictType,
    SourceRef,
    extract_assertion,
)
from agentic_rag_enterprise.evidence.temporal import _naive_utc

if TYPE_CHECKING:
    pass

# "Clear" authority override margin (frozen default; configurable). A gap below
# this threshold must NOT auto-override (build plan §15.3 / contract §5).
AUTHORITY_OVERRIDE_MARGIN = 20

# Modes in which "effective now / at the as_of date" is a meaningful target for
# the temporary-rollback guard (post-filter evidence in these modes is, by
# construction, in force at the target time).
_EFFECTIVE_NOW_MODES = ("current", "unspecified", "as_of")


class ConflictResolver:
    """Resolve candidate conflicts among post-filter Evidence (deterministic)."""

    def __init__(self, *, authority_margin: int = AUTHORITY_OVERRIDE_MARGIN) -> None:
        self.authority_margin = authority_margin

    def resolve(
        self,
        evidence: tuple[SnapshotEvidence, ...] | list[SnapshotEvidence],
        scope: TemporalScope,
        *,
        topic_key: str,
        now: datetime | None = None,
    ) -> ConflictReport:
        """Resolve conflicts over ``evidence`` for ``scope`` grouped by ``topic_key``."""
        del now  # post-filter evidence is already bounded to the scope target
        evs = list(evidence)
        findings: list[ConflictFinding] = []
        dropped: set[str] = set()
        counter = 0

        def next_id() -> str:
            nonlocal counter
            counter += 1
            return f"cf-{counter:03d}"

        # --- Phase 1: version conflicts (same document_id, differing versions) ---
        by_doc: dict[str, list[SnapshotEvidence]] = {}
        for ev in evs:
            by_doc.setdefault(ev.document_id, []).append(ev)
        finding: ConflictFinding | None
        drop: set[str]
        for group in by_doc.values():
            if len({ev.document_version for ev in group}) < 2:
                continue
            finding, drop = self._resolve_version_group(group, next_id, topic_key, scope)
            findings.append(finding)
            dropped.update(drop)

        # --- Phase 2: value / scope conflicts among surviving evidence ----------
        survivors = [ev for ev in evs if ev.evidence_id not in dropped]
        assertions = {ev.evidence_id: extract_assertion(ev.text) for ev in survivors}
        n = len(survivors)
        for i in range(n):
            for j in range(i + 1, n):
                a, b = survivors[i], survivors[j]
                # Pairs already disposed of as version conflicts are skipped.
                if a.document_id == b.document_id and a.document_version != b.document_version:
                    continue
                fa, fb = assertions[a.evidence_id], assertions[b.evidence_id]
                finding, drop = self._resolve_pair(a, b, fa, fb, next_id, topic_key, scope)
                if finding is not None:
                    findings.append(finding)
                    dropped.update(drop)

        if not findings:
            status = ConflictStatus.NONE
        elif any(not f.resolvable for f in findings):
            status = ConflictStatus.CONTRADICTED
        else:
            status = ConflictStatus.RESOLVED

        resolved_ids = tuple(ev.evidence_id for ev in evs if ev.evidence_id not in dropped)
        return ConflictReport(
            scope=scope,
            conflict_status=status,
            findings=tuple(findings),
            resolved_evidence_ids=resolved_ids,
            contradicted_fact_ids=(),
        )

    # --- helpers ---------------------------------------------------------------

    @staticmethod
    def _to_source(ev: SnapshotEvidence) -> SourceRef:
        return SourceRef(
            evidence_id=ev.evidence_id,
            corpus_id=ev.corpus_id,
            document_id=ev.document_id,
            document_version=ev.document_version,
            section_path=ev.section_path,
            source_filename=ev.source_filename,
            authority_level=ev.authority_level,
            effective_from=ev.effective_from,
            effective_to=ev.effective_to,
            is_temporary=ev.effective_to is not None,
        )

    def _resolve_version_group(
        self,
        group: list[SnapshotEvidence],
        next_id,
        topic_key: str,
        scope: TemporalScope,
    ) -> tuple[ConflictFinding, set[str]]:
        sources = tuple(self._to_source(ev) for ev in group)

        # Temporary-rollback guard (rule 2): a bounded/temporary version overlaps
        # a permanent one that is in force at the target time. Do NOT auto-supersede;
        # escalate so both are surfaced.
        temps = [ev for ev in group if ev.effective_to is not None]
        perms = [ev for ev in group if ev.effective_to is None]
        if temps and perms and scope.mode in _EFFECTIVE_NOW_MODES and self._values_differ(group):
            finding = ConflictFinding(
                conflict_id=next_id(),
                conflict_type=ConflictType.TIME_CONFLICT,
                topic_key=topic_key,
                sources=sources,
                resolvable=False,
                resolution=ConflictResolution.UNRESOLVED,
                explanation=(
                    "Temporary version overlaps a permanent version effective now; "
                    "cannot auto-supersede."
                ),
            )
            return finding, set()

        # Rule 1: keep the newest version. Newer = later effective_from; tie-break
        # on lexicographic document_version (documented as imperfect, MVP-only).
        # Normalize effective_from to naive UTC so tz-aware data never collides
        # with the naive datetime.min sentinel during sort.
        ordered = sorted(
            group,
            key=lambda ev: (
                _naive_utc(ev.effective_from) if ev.effective_from is not None else datetime.min,
                ev.document_version,
            ),
        )
        winner = ordered[-1]
        losers = ordered[:-1]
        finding = ConflictFinding(
            conflict_id=next_id(),
            conflict_type=ConflictType.VERSION_CONFLICT,
            topic_key=topic_key,
            sources=sources,
            resolvable=True,
            resolution=ConflictResolution.AUTO_VERSION,
            chosen_evidence_ids=(winner.evidence_id,),
            explanation=f"Newer version {winner.document_version} supersedes older version(s).",
        )
        return finding, {ev.evidence_id for ev in losers}

    def _resolve_pair(
        self,
        a: SnapshotEvidence,
        b: SnapshotEvidence,
        fa,
        fb,
        next_id,
        topic_key: str,
        scope: TemporalScope,
    ) -> tuple[ConflictFinding | None, set[str]]:
        # Pass-through (issue #3): a non-structured evidence means free-text
        # divergence only — never a candidate conflict.
        if not fa.is_structured or not fb.is_structured:
            return None, set()

        same_key = fa.key == fb.key  # None == None → keyless counts as same
        if not same_key:
            # SCOPE_CONFLICT: clearly distinct subjects, complementary not contradictory.
            finding = ConflictFinding(
                conflict_id=next_id(),
                conflict_type=ConflictType.SCOPE_CONFLICT,
                topic_key=topic_key,
                sources=(self._to_source(a), self._to_source(b)),
                resolvable=True,
                resolution=ConflictResolution.AUTO_SCOPE,
                chosen_evidence_ids=(a.evidence_id, b.evidence_id),
                explanation="Distinct keys; complementary, not contradictory.",
            )
            return finding, set()

        if fa.value == fb.value:
            return None, set()  # identical value, no conflict

        # Different values on the same key → VALUE_CONFLICT.
        # Temporary-rollback guard (rule 2): exactly one side is bounded/temporary
        # and both are in force at the target time → escalate, not auto-resolve.
        if (a.effective_to is not None) != (
            b.effective_to is not None
        ) and scope.mode in _EFFECTIVE_NOW_MODES:
            finding = ConflictFinding(
                conflict_id=next_id(),
                conflict_type=ConflictType.TIME_CONFLICT,
                topic_key=topic_key,
                sources=(self._to_source(a), self._to_source(b)),
                resolvable=False,
                resolution=ConflictResolution.UNRESOLVED,
                explanation=(
                    "Temporary rollback overlaps a permanent statement effective now; "
                    "cannot auto-resolve."
                ),
            )
            return finding, set()

        # Rule 3: different sources with a clear authority margin → AUTO_AUTHORITY.
        if (
            a.document_id != b.document_id
            and abs(a.authority_level - b.authority_level) >= self.authority_margin
        ):
            high = a if a.authority_level > b.authority_level else b
            low = b if high is a else a
            finding = ConflictFinding(
                conflict_id=next_id(),
                conflict_type=ConflictType.VALUE_CONFLICT,
                topic_key=topic_key,
                sources=(self._to_source(a), self._to_source(b)),
                resolvable=True,
                resolution=ConflictResolution.AUTO_AUTHORITY,
                chosen_evidence_ids=(high.evidence_id,),
                explanation=(
                    f"Higher authority {high.authority_level} overrides {low.authority_level}."
                ),
            )
            return finding, {low.evidence_id}

        # Rule 4: unresolvable → CONTRADICTED. No winner chosen.
        finding = ConflictFinding(
            conflict_id=next_id(),
            conflict_type=ConflictType.VALUE_CONFLICT,
            topic_key=topic_key,
            sources=(self._to_source(a), self._to_source(b)),
            resolvable=False,
            resolution=ConflictResolution.UNRESOLVED,
            explanation=(
                "Conflicting values from equally-authoritative (or same-source) "
                "evidence; cannot auto-resolve."
            ),
        )
        return finding, set()

    @staticmethod
    def _values_differ(group: list[SnapshotEvidence]) -> bool:
        extracted = [extract_assertion(ev.text) for ev in group]
        for i in range(len(extracted)):
            for j in range(i + 1, len(extracted)):
                vi, vj = extracted[i], extracted[j]
                if vi.is_structured and vj.is_structured:
                    if vi.value != vj.value:
                        return True
                elif vi.is_structured != vj.is_structured:
                    return True
        # Fall back to explicit version divergence.
        return len({ev.document_version for ev in group}) >= 2
