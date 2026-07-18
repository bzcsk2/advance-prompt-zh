"""E-014 shared chat application service (build plan §2.2 / §5 / §6).

One reusable service that backs BOTH the synchronous ``POST /v1/chat`` FastAPI
endpoint and the minimal Gradio adapter. It wires the already-built layers:

* **E-012** ``run_fast_path`` — the one-pass sufficient / insufficient decision
  (exactly one ``retrieve_evidence`` call on the single-pass path);
* **E-011** ``Evidence`` snapshots — the immutable grounding + citation source;
* **E-013** ``build_answer_envelope`` / ``conservative_refusal`` — the typed,
  validated, fail-closed answer envelope;
* **E-019/E-020** ``answer_with_iteration`` — the bounded, gap-driven quality
  iteration loop: it re-judges Required-Fact coverage with a pluggable ``Judge``,
  runs ``GapPlanner`` + ``StopPolicy`` to decide the next retrieval, and only
  then synthesizes (single-corpus; ``answer`` stays the one-pass E-014 path).

The LLM is invoked ONLY here, and only to (a) extract atomic ``Claim``s each
bound to a real ``evidence_id`` and (b) produce a draft prose. Per E-013 the
draft is advisory: the final answer is always derived from the *verified* claims.
Security-context fields (tenant / user / policy / …) are NEVER sent to, or read
back from, the model — they are strictly runtime-injected (build plan §5.4).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Callable, cast

from agentic_rag_enterprise.answer import build_answer_envelope, conservative_refusal
from agentic_rag_enterprise.answer.builder import build_multi_corpus_envelope
from agentic_rag_enterprise.answer.envelope import AnswerEnvelope, TenantBindingError
from agentic_rag_enterprise.corpus.registry import CorpusRegistry
from agentic_rag_enterprise.corpus.router import CorpusRouter
from agentic_rag_enterprise.domain.corpus import CorpusConfig
from agentic_rag_enterprise.domain.evidence import Evidence as SnapshotEvidence
from agentic_rag_enterprise.domain.security import SecurityContext
from agentic_rag_enterprise.judge.claim_evidence_verifier import (
    DeterministicClaimEvidenceVerifier,
)
from agentic_rag_enterprise.judge.gap_planner import GapPlanner
from agentic_rag_enterprise.judge.models import RequiredFact, SufficiencyResult
from agentic_rag_enterprise.judge.protocol import (
    JudgeError,
    JudgeTimeoutError,
)
from agentic_rag_enterprise.judge.query_fact_extractor import (
    DeterministicQueryFactExtractor,
)
from agentic_rag_enterprise.judge.stop_policy import StopPolicy
from agentic_rag_enterprise.providers import ModelProvider
from agentic_rag_enterprise.retrieval.fast_path import (
    FastPathBackendError,
    FastPathResult,
    FastPathSufficiency,
    run_fast_path,
)
from agentic_rag_enterprise.retrieval.models import (
    CorpusNotDiscoverableError,
    ParentAuthorizationError,
    RetrievalBackendError,
)
from agentic_rag_enterprise.security.filter import EmptyAuthorizationScopeError
from agentic_rag_enterprise.retrieval.retriever import SecureRetriever
from agentic_rag_enterprise.retrieval.multi_corpus import (
    CorpusRetrievalFault,
    MultiCorpusRetrieval,
    MultiCorpusResult,
)
from agentic_rag_enterprise.services.claims_schema import ClaimExtraction
from agentic_rag_enterprise.storage.vector_store import DenseEncoder, SparseEncoder

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from agentic_rag_enterprise.judge.protocol import Judge


class ChatServiceError(Exception):
    """Base error for ChatService failures (excludes fast-path backend faults)."""


class ModelInvocationError(ChatServiceError):
    """Raised when the LLM/model provider fails during claim extraction.

    A model outage must surface as a 5xx and must NEVER be relabelled as a
    grounded answer or a conservative refusal (build plan §5.4: the LLM is not a
    security boundary, and a fault is not an answer).
    """


_SYSTEM_PROMPT = (
    "You are a grounded answer extractor for an enterprise RAG system. "
    "You are given a user question and the authorized evidence retrieved for it. "
    "Extract atomic, verifiable claims. Each claim MUST cite one or more "
    "evidence_id values that appear in the provided evidence. Do not invent "
    "evidence ids, and do not add facts that are not supported by the evidence. "
    "Output a short draft answer and the list of claims."
)


def _partial_retrieval_limitations(
    faults: tuple[CorpusRetrievalFault, ...],
) -> tuple[str, ...]:
    """Render an explicit partial-retrieval limitation per faulted corpus (P1-4.3).

    The message names only the faulted ``corpus_id`` (already in the caller's
    authorized route) and never leaks internal error detail beyond the fault type.
    """
    return tuple(
        f"Partial retrieval: corpus {f.corpus_id!r} was unavailable "
        f"({f.error_type}); the answer may be incomplete."
        for f in sorted(faults, key=lambda x: x.corpus_id)
    )


def _merge_results(primary: MultiCorpusResult, extra: MultiCorpusResult) -> MultiCorpusResult:
    """Combine the primary retrieval with a §9.3 fallback expansion (P1-2).

    The fallback corpus was queried *in addition* to (never instead of) the primary,
    so every field is concatenated and ``retrieval_calls`` is summed — giving a
    truthful total call count for ``tool_calls``.
    """
    return MultiCorpusResult(
        evidence=primary.evidence + extra.evidence,
        corpora_used=tuple(sorted(set(primary.corpora_used) | set(extra.corpora_used))),
        routed=tuple(sorted(set(primary.routed) | set(extra.routed))),
        faults=primary.faults + extra.faults,
        insufficient_corpora=tuple(
            sorted(set(primary.insufficient_corpora) | set(extra.insufficient_corpora))
        ),
        retrieval_calls=primary.retrieval_calls + extra.retrieval_calls,
    )


def _evidence_block(evidence: tuple[SnapshotEvidence, ...]) -> str:
    parts: list[str] = []
    for ev in evidence:
        coords = " / ".join(str(p) for p in (ev.corpus_id, ev.document_id, *ev.section_path) if p)
        page = f" p.{ev.page_number}" if ev.page_number is not None else ""
        parts.append(f"[{ev.evidence_id}] {coords}{page}\n{ev.text}")
    return "\n\n".join(parts)


def _build_messages(query: str, evidence: tuple[SnapshotEvidence, ...]) -> list[dict[str, str]]:
    """Build the synthesis prompt. Carries ONLY the query + evidence grounding.

    Security-context fields are deliberately absent — the model must never see
    or produce tenant / identity / policy data (build plan §5.4).
    """
    user = f"Question:\n{query}\n\nAuthorized evidence:\n{_evidence_block(evidence)}"
    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


class ChatService:
    """Synchronous chat / answer service for the single-corpus Internal MVP."""

    def __init__(
        self,
        *,
        retriever: SecureRetriever,
        dense_encoder: DenseEncoder,
        sparse_encoder: SparseEncoder,
        model: ModelProvider,
        resolve_corpus: Callable[[str], CorpusConfig],
        registry: CorpusRegistry | None = None,
        router: CorpusRouter | None = None,
        top_k: int | None = None,
    ) -> None:
        self._retriever = retriever
        self._dense_encoder = dense_encoder
        self._sparse_encoder = sparse_encoder
        self._model = model
        self._resolve_corpus = resolve_corpus
        self._registry = registry
        self._router = router or CorpusRouter()
        self._multi = MultiCorpusRetrieval(retriever)
        self._top_k = top_k

    def answer(
        self,
        query: str,
        ctx: SecurityContext,
        corpus_id: str,
    ) -> AnswerEnvelope:
        """Answer one query over one corpus via the one-pass Fast Path (E-014).

        Equivalent to ``answer_with_iteration(max_rounds=1, judge=None)``: the
        E-019/E-020 judge + loop are not engaged, so all E-014 behaviour
        (including the ``insufficient`` → abstain short-circuit) is preserved.
        """
        return self.answer_with_iteration(query, ctx, corpus_id, max_rounds=1, judge=None)

    def answer_multi_corpus(
        self,
        query: str,
        ctx: SecurityContext,
        *,
        corpus_ids: list[str] | None = None,
        router_limit: int | None = None,
    ) -> AnswerEnvelope:
        """Answer one query across multiple (authorized) corpora — E-016 mode.

        This is an explicit, separate entry from the single-corpus ``answer`` /
        ``answer_with_iteration`` paths: it runs the permission-aware soft router,
        then the cross-corpus retrieval + merge/dedup, then the *single-pass*
        synthesis (no judge, no iteration). The single-corpus Fast Path and the
        E-013/E-019/E-020 envelope all stay untouched.

        Selection:
        * When ``corpus_ids`` is given, the caller pins the corpora (each is still
          resolved + discoverability-checked via ``registry.get``; an
          undiscoverable corpus fails closed).
        * When ``corpus_ids`` is ``None``, the router applies the build-plan §9.3
          policy (high→Top-1, medium→Top-2, low→Top-3) and may additionally expose
          a ``fallback_candidate``. A ``high`` route whose primary returns no
          Evidence is *expanded* to Top-2 (the fallback candidate) before any
          abstain — §9.3 forbids hard-routing a miss straight to "no answer".

        Fault semantics:
        * Security / authorization / binding / configuration errors propagate in
          their original type (fail closed) — never relabelled as a backend fault.
        * A backend fault in one corpus is captured by the retrieval layer and the
          other corpora's evidence is still merged; a *total* fault raises (never
          becomes a refusal). A *partial* fault with at least one surviving corpus
          of evidence answers from that evidence (degraded, with an explicit
          limitation). A partial fault where *no* corpus returned evidence raises —
          it must never become an ordinary ``no_evidence`` abstain (P1-4).
        * Empty merged evidence with no fault yields a conservative refusal
          (abstain lock).

        Args:
            query: The user question.
            ctx: The runtime-injected security context (shared across all corpora).
            corpus_ids: Optional explicit corpus selection. ``None`` → router picks.
            router_limit: Optional hard cap on router candidates (never widens the
                §9.3 policy). ``None`` lets the policy decide Top-1/2/3.
        """
        # 1) Select the corpora to query (router or explicit), all discoverable.
        #    The Registry is the single source of truth for the CorpusConfig that
        #    enters retrieval (P1-2): we use exactly the authorized config
        #    ``registry.get`` returns — never a separate resolver result that could
        #    carry a stale/divergent collection, tenant, ACL or authority.
        if self._registry is None:
            raise ChatServiceError("multi-corpus mode requires a CorpusRegistry on the ChatService")
        if corpus_ids is not None:
            # registry.get fails closed for unknown / non-discoverable ids.
            selected = [self._registry.get(cid, ctx) for cid in corpus_ids]
            fallback: list[CorpusConfig] = []
            allow_fallback = False
        else:
            route = self._router.route(query, ctx, self._registry, limit=router_limit)
            # Re-fetch each routed candidate's authorized config from the registry
            # so the data-plane config matches the control-plane approval exactly.
            selected = [self._registry.get(c.corpus_id, ctx) for c in route.candidates]
            # §9.3 fallback (Top-2 expansion) is ONLY a high-confidence Top-1 route
            # with no explicit hard limit — medium/low are already broad, and a
            # caller's ``router_limit`` must be a hard cap (P1-1).
            allow_fallback = router_limit is None and route.route_confidence == "high"
            fallback = (
                [self._registry.get(c.corpus_id, ctx) for c in route.fallback_candidates]
                if allow_fallback
                else []
            )

        if not selected and not fallback:
            # Nothing discoverable to query → abstain (no evidence, fail-closed).
            # Zero corpora were queried, so tool_calls is 0 (P2-2).
            return build_multi_corpus_envelope(
                ctx,
                query=query,
                evidence=(),
                corpora_used=(),
                answer_markdown="",
                claims=[],
                coverage=None,
                tool_calls=0,
                stop_reason="no_evidence",
            )

        # 2) Cross-corpus retrieval + merge/dedup (raises on total fault or on a
        #    partial fault that left no evidence — never an ordinary abstain).
        result = self._retrieve_or_expand(
            query, ctx, selected, fallback, allow_fallback=allow_fallback
        )

        # Partial-fault (P1-4.3, frozen semantics = degrade + explicit limitation):
        # a routed corpus backend faulted but at least one sibling returned
        # evidence. We answer from the available evidence, but the envelope must
        # carry an explicit partial-retrieval limitation and must not be reported as
        # unconditionally complete/high.
        partial_retrieval = bool(result.faults)
        limitations = _partial_retrieval_limitations(result.faults)

        # 3) Single-pass synthesis from the merged, verified claims.
        return self._synthesize_multi_corpus(
            query,
            ctx,
            result,
            partial_retrieval=partial_retrieval,
            limitations=limitations,
        )

    def _retrieve_or_expand(
        self,
        query: str,
        ctx: SecurityContext,
        selected: list[CorpusConfig],
        fallback: list[CorpusConfig],
        *,
        allow_fallback: bool,
    ) -> MultiCorpusResult:
        """Retrieve across ``selected``; on a high-confidence empty primary, expand.

        Fallback is gated by ``allow_fallback`` (computed by the caller as
        ``corpus_ids is None AND router_limit is None AND route_confidence == high``)
        so medium/low routes and explicit ``router_limit`` caps are never bypassed
        (P1-1). When expanding, ONLY the new fallback corpus is retrieved and its
        result is merged with the primary result — the primary is never re-queried,
        so ``retrieval_calls`` / ``tool_calls`` reflects the true call count (P1-2).

        Security / authorization / binding errors propagate in their original type
        (P1-2): only a genuine ``RetrievalBackendError`` is wrapped as a
        ``FastPathBackendError`` (total outage → 5xx). A partial backend fault that
        leaves no surviving evidence raises — never an ordinary ``no_evidence`` (P1-4).
        """
        try:
            result = self._multi.retrieve(
                ctx,
                query,
                selected,
                top_k=self._top_k,
                dense_encoder=self._dense_encoder,
                sparse_encoder=self._sparse_encoder,
            )
        except (
            CorpusNotDiscoverableError,
            ParentAuthorizationError,
            EmptyAuthorizationScopeError,
            TenantBindingError,
        ):
            # Security / binding / authorization error — propagate in its original
            # type (fail closed); never masked by a healthy sibling corpus.
            raise
        except RetrievalBackendError as exc:
            raise FastPathBackendError(f"multi-corpus retrieval failed: {exc}") from exc

        if (
            not result.evidence
            and allow_fallback
            and fallback
            and set(c.corpus_id for c in fallback) - set(result.routed)
        ):
            # §9.3 single-step expansion: query ONLY the new fallback corpus (Top-2)
            # and merge with the primary result. The primary is not re-queried, so the
            # call count stays truthful (primary × 1 + fallback × 1).
            new_corpus = [c for c in fallback if c.corpus_id not in result.routed]
            try:
                extra = self._multi.retrieve(
                    ctx,
                    query,
                    new_corpus,
                    top_k=self._top_k,
                    dense_encoder=self._dense_encoder,
                    sparse_encoder=self._sparse_encoder,
                )
            except (
                CorpusNotDiscoverableError,
                ParentAuthorizationError,
                EmptyAuthorizationScopeError,
                TenantBindingError,
            ):
                raise
            except RetrievalBackendError as exc:
                raise FastPathBackendError(f"multi-corpus retrieval failed: {exc}") from exc
            result = _merge_results(result, extra)

        if not result.evidence and result.faults:
            # Partial fault with no surviving evidence: this is a backend outage,
            # not a benign "no answer". Raise rather than abstain (P1-4). A total
            # fault would already have raised inside ``retrieve``.
            raise FastPathBackendError(
                f"multi-corpus retrieval partially failed with no evidence: "
                f"{[f.corpus_id for f in result.faults]}"
            )
        return result

    def _synthesize_multi_corpus(
        self,
        query: str,
        ctx: SecurityContext,
        result: MultiCorpusResult,
        *,
        partial_retrieval: bool = False,
        limitations: tuple[str, ...] = (),
    ) -> AnswerEnvelope:
        """Run LLM claim extraction over merged evidence, then build the envelope."""
        messages = _build_messages(query, result.evidence)
        try:
            extraction = cast(
                ClaimExtraction,
                self._model.with_structured_output(ClaimExtraction).invoke(messages),
            )
        except Exception as exc:  # noqa: BLE001 - wrapped as a typed service error
            raise ModelInvocationError(
                f"claim extraction failed for multi-corpus query: {exc}"
            ) from exc

        return build_multi_corpus_envelope(
            ctx,
            query=query,
            evidence=result.evidence,
            corpora_used=result.corpora_used,
            answer_markdown=extraction.draft_answer,
            claims=list(extraction.claims),
            coverage=None,
            tool_calls=result.retrieval_calls,
            limitations=limitations,
            partial_retrieval=partial_retrieval,
            stop_reason="evidence_found",
        )

    def answer_with_iteration(
        self,
        query: str,
        ctx: SecurityContext,
        corpus_id: str,
        *,
        max_rounds: int = 3,
        judge: Judge | None = None,
        required_facts: list[RequiredFact] | None = None,
    ) -> AnswerEnvelope:
        """Answer with the E-019/E-020 bounded, gap-driven quality iteration.

        When ``judge`` is ``None`` this degrades to the single-pass E-014 path
        (``_run_single_pass``) so ``answer`` stays green. When a ``judge`` is
        supplied, the service runs the deterministic loop:

        1. round 0 = ``run_fast_path``; if ``insufficient`` → abstain.
        2. Stage A: ``judge.judge(query, required_facts, evidence)``.
        3. ``StopPolicy`` decides whether to stop (``sufficient`` / ``max_rounds``
           / ``no_new_evidence`` / budget / judge fault) or to run another round.
        4. another round → ``GapPlanner`` sub-queries → ``retrieve_evidence``
           (accumulating Evidence) → re-judge.
        5. after the loop → Stage B ``DeterministicClaimEvidenceVerifier`` then
           ``build_answer_envelope`` with the final ``coverage`` attached.

        A retrieval/infra fault propagates as ``FastPathBackendError``; a judge
        fault (``JudgeTimeoutError`` / ``JudgeError``) degrades conservatively to
        an abstain — it is never relabelled as a grounded answer.

        Args:
            query: The user question.
            ctx: The runtime-injected security context.
            corpus_id: The corpus to answer over (single-corpus in M3).
            max_rounds: Inclusive cap on rounds performed (default 3).
            judge: Optional pluggable ``Judge`` (the deterministic one for the
                Internal MVP). When ``None`` the loop is not engaged.
            required_facts: Explicit Required Facts; when omitted they are derived
                heuristically from the query.
        """
        if judge is None:
            return self._run_single_pass(query, ctx, corpus_id)
        if max_rounds < 1:
            raise ValueError("max_rounds must be >= 1")

        corpus = self._resolve_corpus(corpus_id)

        # Stage A needs Required Facts; derive from the query when none supplied.
        required = list(required_facts or [])
        if not required:
            required = DeterministicQueryFactExtractor().extract(query)

        stop_policy = StopPolicy()
        gap_planner = GapPlanner()
        verifier = DeterministicClaimEvidenceVerifier()

        # Round 0: the single Fast Path retrieval.
        try:
            first_result = run_fast_path(
                self._retriever,
                ctx,
                query,
                corpus,
                top_k=self._top_k,
                dense_encoder=self._dense_encoder,
                sparse_encoder=self._sparse_encoder,
            )
        except FastPathBackendError:
            raise  # retrieval fault must not become a "no answer"

        if first_result.sufficiency is FastPathSufficiency.INSUFFICIENT:
            # No authorized evidence at all → abstain (E-020 step 1). The E-013
            # lock holds: abstain ⇒ stop_reason == no_evidence.
            return conservative_refusal(first_result, ctx, gap_rounds=1, iterations=1, tool_calls=1)

        evidence_by_id: dict[str, SnapshotEvidence] = {
            ev.evidence_id: ev for ev in first_result.evidence
        }
        seen_ids: set[str] = set(evidence_by_id)
        # Seed novelty trackers with round-0 evidence so a repeated gap snapshot
        # (same text / same doc version) is not mistaken for new evidence (P2-2).
        seen_text_hashes: set[str] = {ev.text_hash for ev in first_result.evidence}
        seen_doc_versions: set[tuple[str, str]] = {
            (ev.document_id, ev.document_version) for ev in first_result.evidence
        }
        prior_queries = [query]
        coverage: SufficiencyResult | None = None
        gap_rounds = 0
        retrieval_calls = 1  # round 0 counts as one retrieval pass
        prev_covered: set[str] = set()
        final_reason = first_result.stop_reason.value  # real termination reason (P2-1)

        for round_idx in range(max_rounds):
            gap_rounds = round_idx + 1

            if round_idx == 0:
                # Already retrieved via run_fast_path; everything is "new" this round.
                new_evidence_ids: set[str] = set(seen_ids)
                round_new_content = False
            else:
                # Coverage is always set by round 0's judge call above.
                assert coverage is not None
                plan = gap_planner.plan(coverage, prior_queries=prior_queries, corpus_id=corpus_id)
                # Only the candidate queries not yet executed this loop survive.
                pending = [q for q in plan.queries if q not in prior_queries]
                if not pending:
                    # Every candidate gap query has already been executed; there is
                    # nothing left to retrieve. This round did no work, so it must
                    # NOT be counted as an executed round (P1-2): gap_rounds /
                    # iterations reflect rounds that actually ran retrieval + judge.
                    final_reason = "all_sources_exhausted"
                    gap_rounds = round_idx
                    break
                # Execute exactly ONE new gap query this round (build plan §14.4/§14.5
                # spirit). Running a single query per round lets two *distinct*
                # gainless retrievals span two rounds and reach the `no_new_evidence`
                # stop (P1-1) instead of being collapsed into one round that exhausts
                # every candidate at once.
                q = pending[0]
                new_evidence_ids = set()
                round_new_content = False
                try:
                    evs = self._retriever.retrieve_evidence(
                        ctx,
                        q,
                        corpus,
                        self._top_k,
                        dense_encoder=self._dense_encoder,
                        sparse_encoder=self._sparse_encoder,
                        iteration=round_idx,
                    )
                except Exception as exc:  # noqa: BLE001 - surfaced as a backend fault
                    raise FastPathBackendError(
                        f"gap retrieval failed for corpus {corpus_id!r}: {exc}"
                    ) from exc
                retrieval_calls += 1
                for ev in evs:
                    # Fail-closed: a gap snapshot from another tenant/corpus must
                    # never enter the answer (P1-1). This mirrors the E-013
                    # cross-tenant guard that the M3 accumulation had regressed.
                    if ev.tenant_id != ctx.tenant_id or ev.corpus_id != corpus.corpus_id:
                        raise TenantBindingError(
                            f"gap evidence {ev.evidence_id!r} tenant/corpus "
                            f"({ev.tenant_id}/{ev.corpus_id}) does not match request "
                            f"({ctx.tenant_id}/{corpus.corpus_id})"
                        )
                    is_new_content = (
                        ev.text_hash not in seen_text_hashes
                        or (ev.document_id, ev.document_version) not in seen_doc_versions
                    )
                    existing = evidence_by_id.get(ev.evidence_id)
                    if existing is None:
                        seen_ids.add(ev.evidence_id)
                        evidence_by_id[ev.evidence_id] = ev
                        # P2-2: a brand-new id only counts as a *gain* when it
                        # actually carries new information. A snapshot that merely
                        # re-states already-seen text under a fresh id (e.g. the
                        # same paragraph re-embedded) must NOT reset the no-gain
                        # counter — only a new id with new content/version does.
                        if is_new_content:
                            new_evidence_ids.add(ev.evidence_id)
                    elif (
                        existing.document_version != ev.document_version
                        or existing.text_hash != ev.text_hash
                    ):
                        # Same id but an updated snapshot: keep the latest version
                        # so a new document version / new text is reflected in the
                        # answer (§14.6); the gain signal is `round_new_content`.
                        evidence_by_id[ev.evidence_id] = ev
                    # §14.6 novelty: a new document version or new text hash is a
                    # genuine gain even when the id was already seen (P2-2).
                    if is_new_content:
                        round_new_content = True
                    seen_text_hashes.add(ev.text_hash)
                    seen_doc_versions.add((ev.document_id, ev.document_version))
                if q not in prior_queries:
                    prior_queries.append(q)

            # Stage A: judge coverage over all evidence accumulated so far.
            prev_covered = set(coverage.covered_fact_ids) if coverage else set()
            try:
                coverage = judge.judge(
                    query=query, required_facts=required, evidence=tuple(evidence_by_id.values())
                )
            except (JudgeTimeoutError, JudgeError) as exc:
                # Judge fault: degrade conservatively (abstain), never fabricate.
                logger.warning("coverage judge failed; degrading conservatively: %s", exc)
                return conservative_refusal(
                    first_result,
                    ctx,
                    coverage=SufficiencyResult(
                        overall_status="insufficient",
                        should_abstain=True,
                        fact_coverage=(),
                    ),
                    gap_rounds=gap_rounds,
                    iterations=gap_rounds,
                    tool_calls=retrieval_calls,
                )

            new_covered = set(coverage.covered_fact_ids) - prev_covered

            decision = stop_policy.decide(
                round=round_idx,
                max_rounds=max_rounds,
                overall_status=coverage.overall_status,
                can_continue=coverage.can_continue_retrieval,
                new_evidence_ids=new_evidence_ids,
                new_covered_fact_ids=new_covered,
                judge_ok=True,
                budget_remaining=1.0,
                new_content=round_new_content,
            )
            final_reason = decision.reason  # surface the real stop reason (P2-1)
            if decision.should_stop:
                break

        final_evidence = tuple(evidence_by_id.values())
        return self._synthesize(
            query,
            ctx,
            first_result,
            coverage=coverage,
            verifier=verifier,
            evidence=final_evidence,
            gap_rounds=gap_rounds,
            iterations=gap_rounds,
            tool_calls=retrieval_calls,
            stop_reason=final_reason,
        )

    def _run_single_pass(
        self,
        query: str,
        ctx: SecurityContext,
        corpus_id: str,
    ) -> AnswerEnvelope:
        """The E-014 one-pass path (no judge, no iteration loop).

        Preserves the exact E-014 behaviour: one ``run_fast_path``, the
        ``insufficient`` → abstain short-circuit, and synthesis from verified
        claims. No ``coverage`` is attached.
        """
        corpus = self._resolve_corpus(corpus_id)

        try:
            result = run_fast_path(
                self._retriever,
                ctx,
                query,
                corpus,
                top_k=self._top_k,
                dense_encoder=self._dense_encoder,
                sparse_encoder=self._sparse_encoder,
            )
        except FastPathBackendError:
            raise  # retrieval fault must not become a "no answer"

        if result.sufficiency is FastPathSufficiency.INSUFFICIENT:
            return conservative_refusal(result, ctx)

        return self._synthesize(query, ctx, result)

    def _synthesize(
        self,
        query: str,
        ctx: SecurityContext,
        fast_path_result: FastPathResult,
        *,
        coverage: SufficiencyResult | None = None,
        verifier: DeterministicClaimEvidenceVerifier | None = None,
        evidence: tuple[SnapshotEvidence, ...] | None = None,
        gap_rounds: int = 1,
        iterations: int = 1,
        tool_calls: int = 1,
        stop_reason: str | None = None,
    ) -> AnswerEnvelope:
        """Run LLM claim extraction + Stage B verification, then build the envelope.

        The model prompt carries the (accumulated) evidence so claims can cite it.
        When ``coverage`` is present, Stage B (``DeterministicClaimEvidenceVerifier``)
        assigns each kept claim a ``support_status`` and the verdict is attached.
        ``stop_reason`` (when provided) is the real loop-termination reason and is
        surfaced on non-abstain envelopes (P2-1); abstain envelopes always lock to
        ``no_evidence`` and ignore it.
        """
        synthesis_evidence = evidence if evidence is not None else fast_path_result.evidence
        messages = _build_messages(query, synthesis_evidence)
        try:
            extraction = cast(
                ClaimExtraction,
                self._model.with_structured_output(ClaimExtraction).invoke(messages),
            )
        except Exception as exc:  # noqa: BLE001 - wrapped as a typed service error
            raise ModelInvocationError(
                f"claim extraction failed for corpus {fast_path_result.corpus_id!r}: {exc}"
            ) from exc

        claim_verification = None
        if coverage is not None and verifier is not None:
            claim_verification = verifier.verify(list(extraction.claims), synthesis_evidence)

        return build_answer_envelope(
            fast_path_result,
            ctx,
            answer_markdown=extraction.draft_answer,
            claims=list(extraction.claims),
            coverage=coverage,
            claim_verification=claim_verification,
            gap_rounds=gap_rounds,
            iterations=iterations,
            tool_calls=tool_calls,
            evidence=evidence,
            stop_reason=stop_reason,
        )
