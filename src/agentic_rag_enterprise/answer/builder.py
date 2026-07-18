"""E-013 AnswerEnvelope builder (build plan §7.9 / §16 / §14.7).

Wraps a caller-supplied grounded answer (``answer_markdown`` + ``claims``) into
a typed, validated :class:`AnswerEnvelope`, rendering immutable citations from
the E-011 Evidence Snapshots and running the single deterministic key-claim
support check. When the E-012 Fast Path says ``insufficient`` it produces a
conservative refusal envelope with no fabricated facts.

Safety invariants enforced here (fail-closed):
* the ``SecurityContext`` tenant must match the ``FastPathResult`` tenant and
  the tenant/corpus of every cited Evidence (no cross-tenant leakage);
* unsupported claims never reach the final answer — the answer text is always
  rendered from the *kept* (supported) claims, so an unsupported claim's fact
  cannot appear in the answer; missing or empty claims fail closed to a safe
  partial response;
* ``conservative_refusal`` accepts an ``insufficient`` Fast Path result, or a
  ``sufficient`` result when a coverage verdict says the answer must abstain
  (E-020 coverage-driven abstain); in both cases ``abstained`` locks to
  ``stop_reason == no_evidence``.
"""

from agentic_rag_enterprise.answer.citations import render_citations
from agentic_rag_enterprise.answer.envelope import (
    AnswerEnvelope,
    AnswerEnvelopeError,
    Claim,
    Completeness,
    Confidence,
    TenantBindingError,
)
from agentic_rag_enterprise.answer.verification import (
    ClaimVerificationResult,
    verify_claims,
)
from agentic_rag_enterprise.domain.evidence import Evidence as SnapshotEvidence
from agentic_rag_enterprise.domain.security import SecurityContext
from agentic_rag_enterprise.judge.models import SufficiencyResult
from agentic_rag_enterprise.retrieval.fast_path import (
    FastPathResult,
    FastPathSufficiency,
)

# Conservative refusal wording (build plan §16.2): states missing info, reveals
# no document name or content, fabricates nothing.
_ABSTAIN_MESSAGE = (
    "I cannot answer this reliably: no authorized evidence was found for your question."
)


def _check_tenant_binding(ctx: SecurityContext, result: FastPathResult) -> None:
    """Fail-closed cross-tenant guard (build plan §12.8 / M2 single-tenant)."""
    if ctx.tenant_id != result.tenant_id:
        raise TenantBindingError(
            f"SecurityContext tenant {ctx.tenant_id!r} does not match "
            f"FastPathResult tenant {result.tenant_id!r}"
        )
    for ev in result.evidence:
        if ev.tenant_id != ctx.tenant_id:
            raise TenantBindingError(
                f"Evidence {ev.evidence_id!r} belongs to tenant {ev.tenant_id!r}, "
                f"not {ctx.tenant_id!r}"
            )
        if ev.corpus_id != result.corpus_id:
            raise TenantBindingError(
                f"Evidence {ev.evidence_id!r} belongs to corpus {ev.corpus_id!r}, "
                f"not {result.corpus_id!r}"
            )


def _render_answer_from_claims(claims: list[Claim]) -> str:
    """Render the answer text from the kept (supported) claims only.

    Deriving the answer from the kept claims guarantees that an unsupported
    claim's fact can never appear in the final answer (build plan §16.4).
    """
    if not claims:
        return "No supported claim could be established from the available evidence."
    return "\n".join(claim.text for claim in claims)


def build_answer_envelope(
    fast_path_result: FastPathResult,
    ctx: SecurityContext,
    *,
    answer_markdown: str,
    claims: list[Claim] | None = None,
    coverage: SufficiencyResult | None = None,
    claim_verification: ClaimVerificationResult | None = None,
    gap_rounds: int = 1,
    iterations: int = 1,
    tool_calls: int = 1,
    missing_aspects: tuple[str, ...] | None = None,
    evidence: tuple[SnapshotEvidence, ...] | None = None,
) -> AnswerEnvelope:
    """Build a validated envelope from a Fast Path result and a grounded answer.

    Args:
        fast_path_result: The E-012 one-pass decision (carries the Evidence and
            the ``should_abstain`` signal).
        ctx: The runtime-injected security context (supplies ``request_id`` /
            ``session_id`` and the tenant binding).
        answer_markdown: Caller-supplied draft answer text. It is never returned
            directly; the final answer is rendered from verified claims so
            unsupported facts cannot leak through. The argument remains part of
            the E-014 boundary so the draft and extracted claims travel together.
        claims: The caller-supplied atomic claims to verify and cite.
        coverage: Optional M3 :class:`SufficiencyResult` from the Coverage Judge
            (E-019/E-020). When present it drives ``completeness`` / ``confidence``
            and is attached to the envelope for downstream evaluation.
        claim_verification: Optional M3 Stage B result (per-claim support status).
        gap_rounds / iterations / tool_calls: M3 iteration-loop accounting.
        missing_aspects: Explicit list of missing aspects to surface (defaults to
            the coverage's missing-fact descriptions when ``coverage`` is given).

    Returns:
        A frozen, validated :class:`AnswerEnvelope`. On the ``insufficient``
        branch a conservative refusal envelope is returned instead.

    Raises:
        TenantBindingError: if the context/result/evidence tenants or corpus do
            not match (fail-closed).
    """
    _check_tenant_binding(ctx, fast_path_result)

    if fast_path_result.sufficiency is FastPathSufficiency.INSUFFICIENT:
        return conservative_refusal(
            fast_path_result,
            ctx,
            coverage=coverage,
            gap_rounds=gap_rounds,
            iterations=iterations,
            tool_calls=tool_calls,
        )

    evidence = evidence if evidence is not None else fast_path_result.evidence
    evidence_ids = {ev.evidence_id for ev in evidence}

    verification = claim_verification or verify_claims(claims or [], evidence_ids)
    citations = render_citations(evidence)

    # M3 coverage verdict drives completeness/confidence when available; otherwise
    # fall back to the E-013 claim-removal heuristic.
    if coverage is not None:
        completeness, confidence = _map_coverage_to_completeness(coverage.overall_status)
        if missing_aspects is None:
            missing_aspects = tuple(
                fc.missing_information for fc in coverage.fact_coverage if fc.missing_information
            )
    else:
        if verification.removed_claims or not verification.kept_claims:
            completeness = "partial"
            confidence = "medium"
        else:
            completeness = "complete"
            confidence = "high"

    # An insufficient coverage verdict must abstain (preserves the E-013 lock).
    if completeness == "insufficient":
        return conservative_refusal(
            fast_path_result,
            ctx,
            coverage=coverage,
            gap_rounds=gap_rounds,
            iterations=iterations,
            tool_calls=tool_calls,
        )

    # Always derive the final answer from verified claims. Missing/empty claims
    # fail closed to the generic partial response returned by the renderer.
    final_answer = _render_answer_from_claims(verification.kept_claims)

    return AnswerEnvelope(
        request_id=ctx.request_id,
        session_id=ctx.session_id,
        answer_markdown=final_answer,
        claims=tuple(verification.kept_claims),
        evidence=tuple(evidence),
        citations=tuple(citations),
        completeness=completeness,
        confidence=confidence,
        missing_aspects=missing_aspects or (),
        corpora_used=(fast_path_result.corpus_id,),
        iterations=iterations,
        tool_calls=tool_calls,
        gap_rounds=gap_rounds,
        coverage=coverage,
        stop_reason=fast_path_result.stop_reason.value,
        abstained=False,
    )


def _map_coverage_to_completeness(overall: str) -> tuple[Completeness, Confidence]:
    """Map a Coverage Judge overall verdict to envelope completeness/confidence (§14.7)."""
    mapping: dict[str, tuple[Completeness, Confidence]] = {
        "sufficient": ("complete", "high"),
        "partially_sufficient": ("partial", "medium"),
        "ambiguous": ("partial", "low"),
        "contradicted": ("conflicted", "low"),
        "insufficient": ("insufficient", "low"),
        "policy_blocked": ("insufficient", "low"),
    }
    mapped = mapping.get(overall)
    if mapped is None:
        return ("partial", "medium")
    return mapped


def conservative_refusal(
    fast_path_result: FastPathResult,
    ctx: SecurityContext,
    *,
    coverage: SufficiencyResult | None = None,
    gap_rounds: int = 1,
    iterations: int = 1,
    tool_calls: int = 1,
) -> AnswerEnvelope:
    """Build an abstained refusal envelope for an ``insufficient`` Fast Path result.

    Raises:
        AnswerEnvelopeError: if called with a ``sufficient`` result and no
            coverage verdict (the refusal contract requires an ``insufficient``
            decision, locking ``abstained`` to ``stop_reason == no_evidence``).
            When a coverage verdict is supplied (the E-020 coverage-driven
            abstain) a ``sufficient`` Fast Path result is allowed, because the
            Coverage Judge — not the bare evidence count — decides the answer
            must abstain; the abstain lock (``stop_reason == no_evidence``) is
            still honoured.
        TenantBindingError: if the context/result tenants do not match.
    """
    _check_tenant_binding(ctx, fast_path_result)
    if fast_path_result.sufficiency is not FastPathSufficiency.INSUFFICIENT and coverage is None:
        raise AnswerEnvelopeError("conservative_refusal requires an insufficient FastPathResult")

    missing: tuple[str, ...] = ()
    if coverage is not None:
        missing = tuple(
            fc.missing_information for fc in coverage.fact_coverage if fc.missing_information
        )

    return AnswerEnvelope(
        request_id=ctx.request_id,
        session_id=ctx.session_id,
        answer_markdown=_ABSTAIN_MESSAGE,
        claims=(),
        evidence=(),
        citations=(),
        completeness="insufficient",
        confidence="low",
        missing_aspects=missing,
        corpora_used=(fast_path_result.corpus_id,),
        iterations=iterations,
        tool_calls=tool_calls,
        gap_rounds=gap_rounds,
        coverage=coverage,
        stop_reason="no_evidence",
        abstained=True,
    )
