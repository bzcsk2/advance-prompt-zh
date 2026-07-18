"""E-019/E-020 Judge protocol (build plan §14.1–§14.3).

The Coverage Judge is the boundary between "retrieved Evidence" and "do we have
enough to answer". For the Internal MVP the judge is **deterministic / heuristic**
(see ``deterministic_coverage_judge.py``) and network-free, but every caller
depends only on this :class:`Judge` protocol — so a calibrated LLM judge can be
swapped in later (E-013 defers Judge calibration; E-020 reuses the same seam)
without touching ``services/chat_service.py`` or ``answer/``.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from agentic_rag_enterprise.domain.evidence import Evidence as SnapshotEvidence
from agentic_rag_enterprise.judge.models import CoverageJudgeResult, RequiredFact


class JudgeError(Exception):
    """Base error for judge failures (excludes timeouts)."""


class JudgeTimeoutError(JudgeError):
    """Raised when the judge exceeds its allotted ``timeout``.

    Callers must degrade conservatively (lower confidence / abstain) and log the
    underlying cause — never relabel a timeout as a grounded answer (build plan
    §14.5 scenario 17).
    """


@runtime_checkable
class Judge(Protocol):
    """Coverage Judge contract (Stage A, build plan §14.1–§14.2)."""

    name: str

    def judge(
        self,
        *,
        query: str,
        required_facts: list[RequiredFact],
        evidence: tuple[SnapshotEvidence, ...],
        timeout: float | None = None,
    ) -> CoverageJudgeResult:
        """Judge whether the supplied Evidence covers the Required Facts.

        Must NOT use model/common-sense to fill gaps (§14.2): only the provided
        Evidence determines support. Returns a :class:`CoverageJudgeResult` whose
        ``overall_status`` follows the fixed priority (§14.3).
        """
        ...
