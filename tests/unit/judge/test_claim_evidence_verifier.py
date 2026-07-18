"""Unit tests for E-019 DeterministicClaimEvidenceVerifier (Stage B, §14.1)."""

from datetime import datetime

from agentic_rag_enterprise.answer.envelope import Claim
from agentic_rag_enterprise.domain.evidence import Evidence as SnapshotEvidence
from agentic_rag_enterprise.judge.claim_evidence_verifier import (
    DeterministicClaimEvidenceVerifier,
)


def _ev(evidence_id: str, text: str, tenant_id: str = "t1") -> SnapshotEvidence:
    return SnapshotEvidence(
        evidence_id=evidence_id,
        tenant_id=tenant_id,
        corpus_id="eng",
        document_id="d1",
        document_version="v1",
        source_uri="inline://d1",
        source_filename="d1.md",
        text=text,
        text_hash="h",
        retrieval_query="q",
        authority_level=50,
        retrieved_at=datetime(2024, 1, 1),
        acl_policy_id="default",
        policy_version="1.0",
        retrieval_iteration=0,
    )


def test_entailed_claim_kept() -> None:
    claims = [Claim(claim_id="c1", text="vacation policy grants 20 days", evidence_ids=("e1",))]
    ev = [_ev("e1", "The vacation policy grants 20 days paid leave per year.")]
    res = DeterministicClaimEvidenceVerifier().verify(claims, tuple(ev))
    assert res.removed_claims == []
    assert res.kept_claims[0].support_status == "entailed"


def test_unsupported_claim_removed() -> None:
    claims = [Claim(claim_id="c1", text="secret fact", evidence_ids=("e1",))]
    ev = [_ev("e1", "the weather is sunny")]
    res = DeterministicClaimEvidenceVerifier().verify(claims, tuple(ev))
    assert res.kept_claims == []
    assert res.removed_claims[0].support_status == "unsupported"


def test_contradicted_claim_detected() -> None:
    claims = [Claim(claim_id="c1", text="office in new york", evidence_ids=("e1",))]
    ev = [_ev("e1", "The office is not in new york; it is in boston.")]
    res = DeterministicClaimEvidenceVerifier().verify(claims, tuple(ev))
    assert res.kept_claims[0].support_status == "contradicted"


def test_dangling_evidence_id_removed() -> None:
    claims = [Claim(claim_id="c1", text="x", evidence_ids=("ghost",))]
    ev = [_ev("e1", "vacation policy grants 20 days")]
    res = DeterministicClaimEvidenceVerifier().verify(claims, tuple(ev))
    assert res.kept_claims == []
    assert res.removed_claims[0].support_status == "unsupported"
