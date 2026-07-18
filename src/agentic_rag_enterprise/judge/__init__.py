"""E-019/E-020 Required-Fact Coverage + iteration judge package.

This package is a plain module set — NOT the legacy ``agents`` / ``graph`` M0
mock runtime (build plan §28.2 forbids a second runtime). It defines the
deterministic, pluggable Coverage Judge and the gap-retrieval / stop-policy used
by ``ChatService.answer_with_iteration``.

The ``__init__`` imports ONLY ``models`` and ``protocol`` (which have no
dependency on the ``answer`` layer) to keep the import graph acyclic:
``answer/envelope.py`` imports ``judge.models`` for the optional ``coverage``
field, and the heavier components (``deterministic_coverage_judge``,
``claim_evidence_verifier``) import ``answer.verification`` — so they are imported
from their submodules by callers, not here.
"""

from agentic_rag_enterprise.judge.models import (
    CoverageJudgeResult,
    FactCoverage,
    FactStatus,
    GapRetrievalPlan,
    RequiredFact,
    StopDecision,
    SufficiencyResult,
    build_sufficiency_result,
    derive_overall_status,
)
from agentic_rag_enterprise.judge.protocol import (
    Judge,
    JudgeError,
    JudgeTimeoutError,
)

__all__ = [
    "FactStatus",
    "RequiredFact",
    "FactCoverage",
    "SufficiencyResult",
    "CoverageJudgeResult",
    "GapRetrievalPlan",
    "StopDecision",
    "derive_overall_status",
    "build_sufficiency_result",
    "Judge",
    "JudgeError",
    "JudgeTimeoutError",
]
