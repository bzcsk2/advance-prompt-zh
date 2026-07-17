"""Unit tests for the E-013 answer package (build plan §7.9 / §16).

Hermetic: fake ``Evidence`` snapshots and ``FastPathResult`` objects, no Qdrant
and no LLM. Asserts: tenant binding is enforced fail-closed; unsupported claims
(including explicitly-unsupported and evidence-less critical claims) are removed
and their facts never reach the final answer; ``Claim``/``Citation`` are frozen
and the envelope rejects dangling claim/citation ids; and the abstain state is
locked to ``stop_reason == no_evidence`` (``conservative_refusal`` rejects a
sufficient result).
"""

from datetime import datetime

import pytest

from agentic_rag_enterprise.answer import (
    AnswerEnvelope,
    AnswerEnvelopeError,
    Claim,
    TenantBindingError,
    build_answer_envelope,
    conservative_refusal,
    format_citation_panel,
    render_citations,
    verify_claims,
)
from agentic_rag_enterprise.domain.evidence import Evidence as SnapshotEvidence
from agentic_rag_enterprise.domain.security import SecurityContext
from agentic_rag_enterprise.retrieval.fast_path import (
    FastPathResult,
    FastPathSufficiency,
    FastPathStopReason,
)


def _evidence(
    evidence_id: str = "e1",
    document_id: str = "d1",
    tenant_id: str = "t1",
    corpus_id: str = "eng",
) -> SnapshotEvidence:
    return SnapshotEvidence(
        evidence_id=evidence_id,
        tenant_id=tenant_id,
        corpus_id=corpus_id,
        document_id=document_id,
        document_version="v1",
        source_uri=f"inline://{document_id}",
        source_filename=f"{document_id}.md",
        text="some grounded body",
        text_hash="h",
        retrieval_query="q",
        authority_level=50,
        retrieved_at=datetime(2024, 1, 1),
        acl_policy_id="default",
        policy_version="1.0",
        retrieval_iteration=0,
    )


def _ctx(tenant_id: str = "t1") -> SecurityContext:
    return SecurityContext(
        request_id="r",
        session_id="s",
        tenant_id=tenant_id,
        user_id="u1",
        policy_version="1.0",
    )


def _sufficient(evidence: SnapshotEvidence | None = None) -> FastPathResult:
    ev = evidence or _evidence()
    return FastPathResult(
        query="q",
        corpus_id=ev.corpus_id,
        tenant_id=ev.tenant_id,
        evidence=(ev,),
        sufficiency=FastPathSufficiency.SUFFICIENT,
        stop_reason=FastPathStopReason.EVIDENCE_FOUND,
    )


def _insufficient(tenant_id: str = "t1", corpus_id: str = "eng") -> FastPathResult:
    return FastPathResult(
        query="q",
        corpus_id=corpus_id,
        tenant_id=tenant_id,
        evidence=(),
        sufficiency=FastPathSufficiency.INSUFFICIENT,
        stop_reason=FastPathStopReason.NO_EVIDENCE,
    )


# --- Sufficient path, resolvable citations ---------------------------------


def test_sufficient_path_builds_envelope_with_resolvable_citations() -> None:
    ev = _evidence()
    claims = [Claim(claim_id="C1", text="fact", importance="critical", evidence_ids=("e1",))]
    env = build_answer_envelope(_sufficient(ev), _ctx(), answer_markdown="answer", claims=claims)

    assert env.abstained is False
    assert env.completeness == "complete"
    assert env.confidence == "high"
    assert env.iterations == 1
    assert env.tool_calls == 1
    assert env.corpora_used == ("eng",)
    assert env.stop_reason == "evidence_found"
    assert len(env.evidence) == 1
    assert len(env.claims) == 1

    citations = render_citations([ev])
    assert len(citations) == 1
    cit = citations[0]
    assert cit.index == 1
    assert cit.evidence_id == "e1"
    # Immutable reference fields are carried through.
    assert cit.document_version == "v1"
    assert cit.text_hash == "h"
    assert cit.policy_version == "1.0"
    assert "[1] eng / d1" in format_citation_panel(citations)


# --- Insufficient path: conservative refusal, no fabricated facts -----------


def test_insufficient_path_produces_abstained_refusal() -> None:
    env = build_answer_envelope(_insufficient(), _ctx(), answer_markdown="ignored")

    assert env.abstained is True
    assert env.claims == ()
    assert env.evidence == ()
    assert env.citations == ()
    assert env.completeness == "insufficient"
    assert env.confidence == "low"
    assert env.stop_reason == "no_evidence"
    assert "d1" not in env.answer_markdown
    assert "doc" not in env.answer_markdown
    assert env.answer_markdown.startswith("I cannot answer this reliably")


# --- P1-1: tenant binding enforced fail-closed -----------------------------


def test_tenant_mismatch_between_ctx_and_result_rejected() -> None:
    with pytest.raises(TenantBindingError):
        build_answer_envelope(
            _sufficient(_evidence(tenant_id="ta")),
            _ctx(tenant_id="tb"),
            answer_markdown="a",
            claims=[],
        )


def test_cross_tenant_evidence_rejected() -> None:
    # Result tenant matches ctx, but one Evidence belongs to another tenant.
    ev = _evidence(evidence_id="e1", tenant_id="t1")
    other = _evidence(evidence_id="e2", tenant_id="tx")
    result = FastPathResult(
        query="q",
        corpus_id="eng",
        tenant_id="t1",
        evidence=(ev, other),
        sufficiency=FastPathSufficiency.SUFFICIENT,
        stop_reason=FastPathStopReason.EVIDENCE_FOUND,
    )
    with pytest.raises(TenantBindingError):
        build_answer_envelope(result, _ctx(tenant_id="t1"), answer_markdown="a", claims=[])


def test_cross_corpus_evidence_rejected() -> None:
    ev = _evidence(evidence_id="e1", corpus_id="eng")
    other = _evidence(evidence_id="e2", corpus_id="hr")
    result = FastPathResult(
        query="q",
        corpus_id="eng",
        tenant_id="t1",
        evidence=(ev, other),
        sufficiency=FastPathSufficiency.SUFFICIENT,
        stop_reason=FastPathStopReason.EVIDENCE_FOUND,
    )
    with pytest.raises(TenantBindingError):
        build_answer_envelope(result, _ctx(tenant_id="t1"), answer_markdown="a", claims=[])


# --- P1-2: unsupported claims never enter the final answer ------------------


def test_explicitly_unsupported_claim_removed() -> None:
    ev = _evidence()
    claims = [
        Claim(claim_id="C1", text="supported fact", evidence_ids=("e1",)),
        Claim(
            claim_id="C2",
            text="unsupported fact",
            evidence_ids=("e1",),
            support_status="unsupported",
        ),
    ]
    env = build_answer_envelope(_sufficient(ev), _ctx(), answer_markdown="prose", claims=claims)
    kept_ids = {c.claim_id for c in env.claims}
    assert kept_ids == {"C1"}
    assert "unsupported fact" not in env.answer_markdown
    assert "supported fact" in env.answer_markdown


def test_missing_claims_fail_closed_without_returning_caller_text() -> None:
    env = build_answer_envelope(
        _sufficient(_evidence()),
        _ctx(),
        answer_markdown="UNSUPPORTED CALLER FACT",
        claims=None,
    )

    assert env.claims == ()
    assert env.completeness == "partial"
    assert env.confidence == "medium"
    assert "UNSUPPORTED CALLER FACT" not in env.answer_markdown
    assert (
        env.answer_markdown
        == "No supported claim could be established from the available evidence."
    )


def test_empty_claims_fail_closed_without_returning_caller_text() -> None:
    env = build_answer_envelope(
        _sufficient(_evidence()),
        _ctx(),
        answer_markdown="UNSUPPORTED CALLER FACT",
        claims=[],
    )

    assert env.claims == ()
    assert env.completeness == "partial"
    assert env.confidence == "medium"
    assert "UNSUPPORTED CALLER FACT" not in env.answer_markdown
    assert (
        env.answer_markdown
        == "No supported claim could be established from the available evidence."
    )


def test_critical_claim_with_empty_evidence_downgraded_and_removed() -> None:
    ev = _evidence()
    claims = [Claim(claim_id="C1", text="unsupported fact", importance="critical", evidence_ids=())]
    env = build_answer_envelope(_sufficient(ev), _ctx(), answer_markdown="prose", claims=claims)
    assert env.claims == ()
    assert env.completeness == "partial"
    assert env.confidence == "medium"
    assert "unsupported fact" not in env.answer_markdown


def test_dangling_evidence_id_claim_removed() -> None:
    ev = _evidence()
    claims = [
        Claim(
            claim_id="C1", text="unsupported fact", importance="critical", evidence_ids=("ghost",)
        )
    ]
    env = build_answer_envelope(_sufficient(ev), _ctx(), answer_markdown="prose", claims=claims)
    assert env.claims == ()
    assert env.completeness == "partial"


def test_verify_claims_marks_unsupported() -> None:
    claims = [
        Claim(claim_id="C1", text="x", evidence_ids=("e1",)),
        Claim(claim_id="C2", text="y", evidence_ids=(), importance="critical"),
        Claim(claim_id="C3", text="z", support_status="unsupported"),
    ]
    res = verify_claims(claims, {"e1"})
    assert {c.claim_id for c in res.kept_claims} == {"C1"}
    assert {c.claim_id for c in res.removed_claims} == {"C2", "C3"}
    assert res.any_critical_unsupported is True


# --- P1-3: deep freeze + citation reference check ---------------------------


def test_claim_and_citation_are_frozen() -> None:
    claim = Claim(claim_id="C1", text="x", evidence_ids=("e1",))
    with pytest.raises(Exception):
        claim.evidence_ids = ("e2",)  # type: ignore[misc]
    cit = render_citations([_evidence()])[0]
    with pytest.raises(Exception):
        cit.document_version = "v9"  # type: ignore[misc]


def test_dangling_claim_rejected_by_validator() -> None:
    ev = _evidence()
    with pytest.raises(ValueError):
        AnswerEnvelope(
            request_id="r",
            session_id="s",
            answer_markdown="a",
            claims=(Claim(claim_id="C1", text="x", evidence_ids=("ghost",)),),
            evidence=(ev,),
            completeness="complete",
            confidence="high",
            stop_reason="evidence_found",
            abstained=False,
        )


def test_dangling_citation_rejected_by_validator() -> None:
    ev = _evidence()
    # Manually craft a citation whose evidence_id is absent from the evidence set.
    from agentic_rag_enterprise.answer.envelope import Citation

    bad_citation = Citation(
        index=1,
        evidence_id="ghost",
        corpus_id="eng",
        document_id="d1",
        document_version="v1",
        text_hash="h",
        retrieved_at="2024-01-01T00:00:00",
        policy_version="1.0",
    )
    with pytest.raises(ValueError):
        AnswerEnvelope(
            request_id="r",
            session_id="s",
            answer_markdown="a",
            claims=(),
            evidence=(ev,),
            citations=(bad_citation,),
            completeness="complete",
            confidence="high",
            stop_reason="evidence_found",
            abstained=False,
        )


# --- P1-4: abstain state locked to no_evidence ------------------------------


def test_conservative_refusal_rejects_sufficient_result() -> None:
    with pytest.raises(AnswerEnvelopeError):
        conservative_refusal(_sufficient(_evidence()), _ctx())


def test_abstained_envelope_state_locked() -> None:
    ev = _evidence()
    # abstained but carrying claims/evidence -> rejected.
    with pytest.raises(ValueError):
        AnswerEnvelope(
            request_id="r",
            session_id="s",
            answer_markdown="a",
            claims=(Claim(claim_id="C1", text="x"),),
            evidence=(ev,),
            completeness="insufficient",
            confidence="low",
            stop_reason="no_evidence",
            abstained=True,
        )
    # abstained but wrong stop_reason -> rejected.
    with pytest.raises(ValueError):
        AnswerEnvelope(
            request_id="r",
            session_id="s",
            answer_markdown="a",
            claims=(),
            evidence=(),
            completeness="insufficient",
            confidence="low",
            stop_reason="evidence_found",
            abstained=True,
        )
    # completeness 'insufficient' requires abstained=True.
    with pytest.raises(ValueError):
        AnswerEnvelope(
            request_id="r",
            session_id="s",
            answer_markdown="a",
            claims=(),
            evidence=(),
            completeness="insufficient",
            confidence="low",
            stop_reason="no_evidence",
            abstained=False,
        )
    # conservative_refusal always yields a valid locked envelope.
    env = conservative_refusal(_insufficient(), _ctx())
    assert env.abstained is True
    assert env.completeness == "insufficient"
    assert env.stop_reason == "no_evidence"
