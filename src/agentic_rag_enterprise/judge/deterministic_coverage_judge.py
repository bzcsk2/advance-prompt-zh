"""E-019 deterministic, heuristic Coverage Judge (Stage A, build plan §14.1–§14.3).

This is the Internal-MVP judge: **no LLM, no network**. It decides whether the
retrieved Evidence covers each Required Fact by lexical token overlap, with a
light contradiction heuristic. The verdict follows the fixed fact-status priority
(§14.3). It implements the :class:`~agentic_rag_enterprise.judge.protocol.Judge`
protocol, so a calibrated LLM judge can replace it later without touching the
service or the answer layer.

The judge must NOT use model/common-sense to fill gaps (§14.2): support is
determined only by the Evidence text actually supplied.
"""

from __future__ import annotations

import re

from agentic_rag_enterprise.domain.evidence import Evidence as SnapshotEvidence
from agentic_rag_enterprise.judge.models import (
    FactCoverage,
    FactStatus,
    RequiredFact,
    SufficiencyResult,
    build_sufficiency_result,
)

# Tokens below this length and the stopword set are dropped before matching so
# that function words do not create false overlaps.
_MIN_TOKEN_LEN = 3
_STOPWORDS = frozenset(
    {
        "the",
        "and",
        "for",
        "with",
        "that",
        "this",
        "from",
        "are",
        "was",
        "were",
        "have",
        "has",
        "been",
        "will",
        "can",
        "should",
        "would",
        "which",
        "what",
        "when",
        "where",
        "who",
        "why",
        "how",
        "into",
        "than",
        "then",
        "they",
        "their",
        "such",
        "also",
        "each",
        "more",
        "most",
        "some",
        "used",
        "using",
        "via",
        "per",
        "does",
        "doesn",
        "not",
    }
)
_NEGATIONS = frozenset(
    {
        "not",
        "no",
        "never",
        "cannot",
        "cant",
        "without",
        "false",
        "incorrect",
        "wrong",
        "denied",
        "rejected",
        "isn",
        "wasn",
        "weren",
        "doesn",
        "don",
    }
)
_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_]+")


def _tokenize(text: str) -> list[str]:
    return [
        t
        for t in _TOKEN_RE.findall(text.lower())
        if len(t) >= _MIN_TOKEN_LEN and t not in _STOPWORDS
    ]


class DeterministicCoverageJudge:
    """Heuristic Coverage Judge (pluggable ``Judge`` implementation)."""

    name = "deterministic"

    def judge(
        self,
        *,
        query: str,
        required_facts: list[RequiredFact],
        evidence: tuple[SnapshotEvidence, ...],
        timeout: float | None = None,
    ) -> SufficiencyResult:
        coverages: list[FactCoverage] = []
        for fact in required_facts:
            coverages.append(self._judge_fact(fact, evidence))

        required_ids = {f.fact_id for f in required_facts if f.required}
        return build_sufficiency_result(coverages=coverages, required_fact_ids=required_ids)

    def _judge_fact(
        self, fact: RequiredFact, evidence: tuple[SnapshotEvidence, ...]
    ) -> FactCoverage:
        fact_tokens = set(_tokenize(fact.description))
        if not fact_tokens:
            # Degenerate fact (all stopwords) — treat as supported to avoid
            # spuriously blocking the answer.
            return FactCoverage(
                fact_id=fact.fact_id,
                status=FactStatus.SUPPORTED,
                required=fact.required,
                explanation="fact had no matchable tokens",
            )

        best_status = FactStatus.MISSING
        best_evidence_ids: list[str] = []
        best_matched = 0
        contradiction = False

        for ev in evidence:
            ev_tokens = set(_tokenize(ev.text))
            matched = fact_tokens & ev_tokens
            if not matched:
                continue
            # Contradiction heuristic: a negation token sits among the matched
            # keywords in the evidence sentence → the fact is refuted, not supported.
            if self._has_negation_near(ev.text, matched):
                contradiction = True
            if len(matched) > best_matched:
                best_matched = len(matched)
                best_evidence_ids = [ev.evidence_id]

        if contradiction:
            best_status = FactStatus.CONTRADICTED
        elif best_matched == 0:
            best_status = FactStatus.NOT_RETRIEVABLE if not evidence else FactStatus.MISSING
        elif best_matched >= max(1, int(0.5 * len(fact_tokens))):
            best_status = FactStatus.SUPPORTED
        else:
            best_status = FactStatus.PARTIALLY_SUPPORTED

        if not evidence and best_status is FactStatus.MISSING:
            best_status = FactStatus.NOT_RETRIEVABLE

        next_queries: tuple[str, ...] = ()
        if best_status in (FactStatus.MISSING, FactStatus.PARTIALLY_SUPPORTED):
            next_queries = (fact.description,)

        return FactCoverage(
            fact_id=fact.fact_id,
            status=best_status,
            required=fact.required,
            evidence_ids=tuple(best_evidence_ids),
            explanation=(
                f"{len(best_evidence_ids)} evidence(s) matched "
                f"{best_matched}/{len(fact_tokens)} tokens"
            ),
            missing_information=(fact.description if best_status is FactStatus.MISSING else None),
            next_queries=next_queries,
        )

    @staticmethod
    def _has_negation_near(text: str, matched: set[str]) -> bool:
        tokens = _TOKEN_RE.findall(text.lower())
        matched_near_neg = False
        for i, tok in enumerate(tokens):
            if tok in _NEGATIONS:
                window = tokens[max(0, i - 3) : i + 4]
                if window and any(m in window for m in matched):
                    matched_near_neg = True
                    break
        return matched_near_neg
