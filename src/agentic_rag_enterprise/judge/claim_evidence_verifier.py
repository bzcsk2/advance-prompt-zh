"""E-019 deterministic Claim-Evidence Verifier (Stage B, build plan §14.1).

Runs *after* synthesis: for each kept atomic ``Claim`` it checks whether the
Evidence it cites actually supports the claim text (lexical overlap + a light
contradiction heuristic), assigning a ``support_status``. This is the Internal-
MVP, network-free version of the richer multi-model Claim Verifier E-013 defers;
it reuses the same ``ClaimVerificationResult`` shape as ``answer.verification``.
"""

from __future__ import annotations

from agentic_rag_enterprise.answer.envelope import Claim
from agentic_rag_enterprise.answer.verification import ClaimVerificationResult
from agentic_rag_enterprise.domain.evidence import Evidence as SnapshotEvidence
from agentic_rag_enterprise.judge.deterministic_coverage_judge import (
    _NEGATIONS,
    _TOKEN_RE,
    _tokenize,
)

_ClaimSupport = str  # literal handled by Claim.support_status


class DeterministicClaimEvidenceVerifier:
    """Stage B verifier: assigns each kept claim a ``support_status``."""

    name = "deterministic"

    def verify(
        self,
        claims: list[Claim],
        evidence: tuple[SnapshotEvidence, ...],
    ) -> ClaimVerificationResult:
        evidence_ids = {ev.evidence_id for ev in evidence}
        evidence_by_id = {ev.evidence_id: ev for ev in evidence}

        kept: list[Claim] = []
        removed: list[Claim] = []
        any_critical_unsupported = False

        for claim in claims:
            unresolved = [eid for eid in claim.evidence_ids if eid not in evidence_ids]
            if claim.support_status == "unsupported" or not claim.evidence_ids or unresolved:
                updated = claim.model_copy(update={"support_status": "unsupported"})
                removed.append(updated)
                if updated.importance == "critical":
                    any_critical_unsupported = True
                continue

            status = self._check(claim, evidence_by_id)
            if status == "unsupported":
                # A claim whose cited evidence does not support it (no lexical
                # overlap) must be removed, never rendered into the answer.
                updated = claim.model_copy(update={"support_status": "unsupported"})
                removed.append(updated)
                if updated.importance == "critical":
                    any_critical_unsupported = True
                continue
            kept.append(claim.model_copy(update={"support_status": status}))

        return ClaimVerificationResult(
            kept_claims=kept,
            removed_claims=removed,
            any_critical_unsupported=any_critical_unsupported,
        )

    def _check(self, claim: Claim, evidence_by_id: dict[str, SnapshotEvidence]) -> _ClaimSupport:
        claim_tokens = set(_tokenize(claim.text))
        if not claim_tokens:
            return "unsupported"

        best_matched = 0
        contradicted = False
        for eid in claim.evidence_ids:
            ev = evidence_by_id.get(eid)
            if ev is None:
                continue
            ev_tokens = set(_tokenize(ev.text))
            matched = claim_tokens & ev_tokens
            if not matched:
                continue
            if self._has_negation_near(ev.text, matched):
                contradicted = True
            if len(matched) > best_matched:
                best_matched = len(matched)

        if contradicted:
            return "contradicted"
        if best_matched >= max(1, int(0.5 * len(claim_tokens))):
            return "entailed"
        if best_matched > 0:
            return "partially_entailed"
        return "unsupported"

    @staticmethod
    def _has_negation_near(text: str, matched: set[str]) -> bool:
        # Keep negation tokens (``not`` is a stopword for overlap but must be
        # retained here so contradictions can be detected) — mirror the Coverage
        # Judge's heuristic.
        tokens = _TOKEN_RE.findall(text.lower())
        for i, tok in enumerate(tokens):
            if tok in _NEGATIONS:
                window = tokens[max(0, i - 3) : i + 4]
                if any(m in window for m in matched):
                    return True
        return False
