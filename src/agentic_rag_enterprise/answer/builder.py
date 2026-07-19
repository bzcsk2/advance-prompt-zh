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
from agentic_rag_enterprise.evidence.models import ConflictReport, ConflictStatus
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


def _check_evidence_binding(
    ctx: SecurityContext, evidence: tuple[SnapshotEvidence, ...], corpus_id: str
) -> None:
    """Fail-closed guard over an *arbitrary* Evidence collection (build plan §12.8).

    The M3 iteration loop accumulates Evidence across rounds (gap-retrieval) and
    passes it via the ``evidence=`` override. That accumulated set must be bound to
    the request tenant and the answered corpus exactly like the Fast Path evidence
    — a cross-tenant / cross-corpus snapshot must never enter the final envelope
    (this restores the E-013 fail-closed invariant that the M3 loop had regressed).
    """
    for ev in evidence:
        if ev.tenant_id != ctx.tenant_id:
            raise TenantBindingError(
                f"Evidence {ev.evidence_id!r} belongs to tenant {ev.tenant_id!r}, "
                f"not {ctx.tenant_id!r}"
            )
        if ev.corpus_id != corpus_id:
            raise TenantBindingError(
                f"Evidence {ev.evidence_id!r} belongs to corpus {ev.corpus_id!r}, not {corpus_id!r}"
            )


def _render_answer_from_claims(claims: list[Claim]) -> str:
    """Render the answer text from the kept (supported) claims only.

    Deriving the answer from the kept claims guarantees that an unsupported
    claim's fact can never appear in the final answer (build plan §16.4).
    """
    if not claims:
        return "No supported claim could be established from the available evidence."
    return "\n".join(claim.text for claim in claims)


def render_conflict_answer(
    report: ConflictReport,
    evidence: tuple[SnapshotEvidence, ...],
) -> str:
    """Deterministic CONTRADICTED answer text, independent of the LLM draft (P1-3).

    Renders directly from ``ConflictReport.findings`` so the final answer lists
    every conflicting source with its document/version and effective window even
    when the model returns no claims or ignores the prompt instruction. No single
    conclusion is asserted — the user is shown both positions and their times.
    """
    ev_by_id = {ev.evidence_id: ev for ev in evidence}
    lines: list[str] = [
        "The available evidence contradicts itself on this question. "
        "Both positions are presented below; no single answer is asserted."
    ]
    for finding in report.findings:
        if finding.resolvable:
            # Only unresolved / contradicted findings surface to the user; an
            # auto-resolved version/authority finding is not a live contradiction.
            continue
        lines.append("")
        lines.append(f"Conflict type: {finding.conflict_type.value}.")
        for src in finding.sources:
            ef = src.effective_from.isoformat() if src.effective_from else "unknown"
            et = src.effective_to.isoformat() if src.effective_to else "open-ended"
            snap = ev_by_id.get(src.evidence_id)
            text = snap.text if snap is not None else "(evidence text unavailable)"
            lines.append(
                f"- Source {src.evidence_id} (document {src.document_id}, "
                f"version {src.document_version}, effective {ef} → {et}): {text}"
            )
    return "\n".join(lines)


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
    stop_reason: str | None = None,
    conflict_report: ConflictReport | None = None,
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

    effective_evidence = evidence if evidence is not None else fast_path_result.evidence
    # Fail-closed binding over the *effective* (possibly gap-accumulated) Evidence.
    # The single-pass path re-validates the same Fast Path evidence; the M3 loop
    # re-validates the accumulated set so a cross-tenant/cross-corpus gap snapshot
    # can never reach the envelope (P1-1).
    _check_evidence_binding(ctx, effective_evidence, fast_path_result.corpus_id)

    evidence = effective_evidence

    return _build_envelope_from_evidence(
        ctx,
        evidence,
        claims or [],
        claim_verification=claim_verification,
        coverage=coverage,
        gap_rounds=gap_rounds,
        iterations=iterations,
        tool_calls=tool_calls,
        missing_aspects=missing_aspects,
        stop_reason=stop_reason,
        abstain_stop_reason=fast_path_result.stop_reason.value,
        corpora_used=(fast_path_result.corpus_id,),
        answer_markdown=answer_markdown,
        conflict_report=conflict_report,
    )


def build_multi_corpus_envelope(
    ctx: SecurityContext,
    *,
    query: str,
    evidence: tuple[SnapshotEvidence, ...],
    corpora_used: tuple[str, ...],
    answer_markdown: str,
    claims: list[Claim] | None = None,
    coverage: SufficiencyResult | None = None,
    claim_verification: ClaimVerificationResult | None = None,
    gap_rounds: int = 1,
    iterations: int = 1,
    tool_calls: int = 1,
    missing_aspects: tuple[str, ...] | None = None,
    limitations: tuple[str, ...] = (),
    partial_retrieval: bool = False,
    stop_reason: str | None = None,
    conflict_report: ConflictReport | None = None,
) -> AnswerEnvelope:
    """Build a validated envelope from merged multi-corpus Evidence (E-016).

    Mirrors :func:`build_answer_envelope` but binds across *multiple* corpora
    (``corpora_used`` + a multi-corpus tenant binding) instead of a single
    Fast Path corpus. The merged Evidence must be bound to the request tenant and
    to the set of contributing corpora — a cross-tenant / cross-corpus snapshot
    must never reach the envelope (same fail-closed invariant as the single-corpus
    path, extended to the E-016 multi-corpus merge).
    """
    for ev in evidence:
        if ev.tenant_id != ctx.tenant_id:
            raise TenantBindingError(
                f"Evidence {ev.evidence_id!r} belongs to tenant {ev.tenant_id!r}, "
                f"not {ctx.tenant_id!r}"
            )
        if ev.corpus_id not in corpora_used:
            raise TenantBindingError(
                f"Evidence {ev.evidence_id!r} belongs to corpus {ev.corpus_id!r}, "
                f"not in the contributing set {corpora_used!r}"
            )

    # Empty merged evidence → conservative refusal (abstain lock). The single-pass
    # multi-corpus path never fabricates an answer from nothing.
    if not evidence:
        return _build_refusal(
            ctx,
            corpora_used=corpora_used,
            coverage=coverage,
            gap_rounds=gap_rounds,
            iterations=iterations,
            tool_calls=tool_calls,
        )

    return _build_envelope_from_evidence(
        ctx,
        evidence,
        claims or [],
        claim_verification=claim_verification,
        coverage=coverage,
        gap_rounds=gap_rounds,
        iterations=iterations,
        tool_calls=tool_calls,
        missing_aspects=missing_aspects,
        limitations=limitations,
        partial_retrieval=partial_retrieval,
        stop_reason=stop_reason,
        # Multi-corpus abstain lock always uses the canonical no_evidence reason.
        abstain_stop_reason="no_evidence",
        corpora_used=corpora_used,
        answer_markdown=answer_markdown,
        conflict_report=conflict_report,
    )


def _build_envelope_from_evidence(
    ctx: SecurityContext,
    evidence: tuple[SnapshotEvidence, ...],
    claims: list[Claim],
    *,
    claim_verification: ClaimVerificationResult | None,
    coverage: SufficiencyResult | None,
    gap_rounds: int,
    iterations: int,
    tool_calls: int,
    missing_aspects: tuple[str, ...] | None,
    stop_reason: str | None,
    abstain_stop_reason: str,
    corpora_used: tuple[str, ...],
    answer_markdown: str,
    limitations: tuple[str, ...] = (),
    partial_retrieval: bool = False,
    conflict_report: ConflictReport | None = None,
) -> AnswerEnvelope:
    """Shared synthesis core for the single- and multi-corpus envelope builders."""
    evidence_ids = {ev.evidence_id for ev in evidence}

    verification = claim_verification or verify_claims(claims, evidence_ids)
    citations = render_citations(evidence)

    # M3 coverage verdict drives completeness/confidence when available; otherwise
    # fall back to the E-013 claim-removal heuristic.
    if coverage is not None:
        completeness, confidence = _map_coverage_to_completeness(coverage.overall_status)
        # Stage B (Claim-Evidence Verifier) can downgrade a Stage-A "complete".
        # A `complete` answer with no surviving verified claim, or with a removed
        # critical claim, would regress the E-013 fail-closed rule — so it is
        # forced down to partial/low (P1-2). Already-partial / conflicted / ambiguous
        # verdicts are left as the Coverage Judge set them.
        if verification is not None and completeness == "complete":
            if not verification.kept_claims or verification.any_critical_unsupported:
                completeness, confidence = "partial", "low"
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

    # E-021 (fail-closed): a CONTRADICTED conflict report forces a "conflicted"
    # envelope regardless of any coverage verdict — the answer must enumerate
    # both conflicting sources/times. This overrides the completeness derived
    # above and is independent of whether a coverage verdict was supplied.
    if (
        conflict_report is not None
        and conflict_report.conflict_status == ConflictStatus.CONTRADICTED
    ):
        completeness = "conflicted"
        confidence = "low"

    # An insufficient coverage verdict must abstain (preserves the E-013 lock).
    if completeness == "insufficient":
        return _build_refusal(
            ctx,
            corpora_used=corpora_used,
            coverage=coverage,
            gap_rounds=gap_rounds,
            iterations=iterations,
            tool_calls=tool_calls,
        )

    # Partial retrieval (E-016 degraded mode, P1-4.3): some routed corpus backend
    # faulted but a sibling returned evidence. The answer may be built from the
    # available evidence, but it must NOT be reported as unconditionally
    # complete/high, and the limitation is surfaced explicitly on the envelope.
    if partial_retrieval and completeness == "complete":
        completeness, confidence = "partial", "medium"

    # E-021 (P1-3): a CONTRADICTED report yields a deterministic answer rendered
    # from the conflict findings alone — never from the model draft or extracted
    # claims — so the final answer lists both sources + times even when the model
    # returns no claims or ignores the instruction. No single conclusion is
    # asserted, so the conflicted envelope carries no claims.
    if (
        conflict_report is not None
        and conflict_report.conflict_status == ConflictStatus.CONTRADICTED
    ):
        final_answer = render_conflict_answer(conflict_report, evidence)
        final_claims: tuple[Claim, ...] = ()
    else:
        final_answer = _render_answer_from_claims(verification.kept_claims)
        final_claims = tuple(verification.kept_claims)

    # For a non-abstain envelope the real loop-termination reason (max_rounds /
    # no_new_evidence / all_sources_exhausted / sufficient / continue) is surfaced
    # when provided; otherwise the Fast Path's reason is used (P2-1). The abstain
    # lock always forces stop_reason == no_evidence and is never overridden here.
    final_stop_reason = stop_reason if stop_reason is not None else abstain_stop_reason

    return AnswerEnvelope(
        request_id=ctx.request_id,
        session_id=ctx.session_id,
        answer_markdown=final_answer,
        claims=final_claims,
        evidence=tuple(evidence),
        citations=tuple(citations),
        completeness=completeness,
        confidence=confidence,
        missing_aspects=missing_aspects or (),
        limitations=limitations,
        corpora_used=corpora_used,
        iterations=iterations,
        tool_calls=tool_calls,
        gap_rounds=gap_rounds,
        coverage=coverage,
        stop_reason=final_stop_reason,
        abstained=False,
        conflict_report=conflict_report,
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


def _build_refusal(
    ctx: SecurityContext,
    *,
    corpora_used: tuple[str, ...],
    coverage: SufficiencyResult | None = None,
    gap_rounds: int = 1,
    iterations: int = 1,
    tool_calls: int = 1,
) -> AnswerEnvelope:
    """Internal abstain builder shared by single- and multi-corpus paths.

    Unlike the public :func:`conservative_refusal`, this does not require a
    ``FastPathResult`` — it binds directly to ``ctx`` and the contributing
    ``corpora_used`` (which, for multi-corpus, may be several corpora). The abstain
    lock (``stop_reason == no_evidence``) is always honoured.
    """
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
        corpora_used=corpora_used,
        iterations=iterations,
        tool_calls=tool_calls,
        gap_rounds=gap_rounds,
        coverage=coverage,
        stop_reason="no_evidence",
        abstained=True,
    )


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


def build_no_evidence_refusal(
    ctx: SecurityContext,
    *,
    corpora_used: tuple[str, ...],
    tool_calls: int = 1,
    gap_rounds: int = 1,
    iterations: int = 1,
) -> AnswerEnvelope:
    """Conservative refusal when no evidence survives retrieval + temporal filtering (P1-1).

    Each ChatService path calls this *after* the E-021 temporal filter drops
    *all* evidence (everything expired / not-yet-effective / outside the
    historical window). The model is never invoked. The envelope locks to
    ``completeness == insufficient`` / ``abstained is True`` /
    ``stop_reason == no_evidence``.
    """
    return _build_refusal(
        ctx,
        corpora_used=corpora_used,
        tool_calls=tool_calls,
        gap_rounds=gap_rounds,
        iterations=iterations,
    )
