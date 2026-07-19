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
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, cast

from agentic_rag_enterprise.answer import build_answer_envelope, conservative_refusal
from agentic_rag_enterprise.answer.builder import (
    build_multi_corpus_envelope,
    build_no_evidence_refusal,
)
from agentic_rag_enterprise.answer.envelope import AnswerEnvelope, TenantBindingError
from agentic_rag_enterprise.corpus.registry import CorpusRegistry
from agentic_rag_enterprise.corpus.router import CorpusRouter
from agentic_rag_enterprise.domain.corpus import CorpusConfig
from agentic_rag_enterprise.domain.evidence import Evidence as SnapshotEvidence
from agentic_rag_enterprise.domain.security import SecurityContext
from agentic_rag_enterprise.domain.temporal import TemporalScope, parse_temporal_scope
from agentic_rag_enterprise.evidence.conflict_resolver import ConflictResolver
from agentic_rag_enterprise.evidence.models import (
    ConflictReport,
    ConflictStatus,
    normalize_topic_key,
)
from agentic_rag_enterprise.evidence.temporal import filter_by_temporal_scope
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
from agentic_rag_enterprise.storage.checkpoint_store import (
    CHECKPOINT_ABORTED,
    CHECKPOINT_COMPLETED,
    RunCheckpoint,
    ResumeAuthError,
    reauthorize_evidence,
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
    from agentic_rag_enterprise.corpus.registry import CorpusRegistry
    from agentic_rag_enterprise.judge.protocol import Judge
    from agentic_rag_enterprise.storage.metadata_store import MetadataStore


class ChatServiceError(Exception):
    """Base error for ChatService failures (excludes fast-path backend faults)."""


class ModelInvocationError(ChatServiceError):
    """Raised when the LLM/model provider fails during claim extraction.

    A model outage must surface as a 5xx and must NEVER be relabelled as a
    grounded answer or a conservative refusal (build plan §5.4: the LLM is not a
    security boundary, and a fault is not an answer).
    """


@dataclass
class _IterationState:
    """Mutable working copy of the iteration-loop accumulators.

    Rebuilt from a :class:`RunCheckpoint` on resume; the frozen checkpoint itself
    is never mutated in place.
    """

    evidence_by_id: dict[str, SnapshotEvidence]
    seen_ids: set[str]
    seen_text_hashes: set[str]
    seen_doc_versions: set[tuple[str, str]]
    prior_queries: list[str]
    coverage: SufficiencyResult | None
    gap_rounds: int
    retrieval_calls: int
    prev_covered: set[str]
    final_reason: str | None
    scope: TemporalScope
    final_report: ConflictReport | None
    final_evidence: tuple[SnapshotEvidence, ...]
    conflict_stop: bool


class _BreakLoop(Exception):
    """Internal control-flow signal: stop the iteration loop (equivalent to the
    original ``break``). Used for normal stop reasons — conflict detected,
    ``no_new_evidence``, ``all_sources_exhausted`` — where the loop ends but the
    answer is still finalized from ``state`` (not returned mid-round)."""


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


def _build_messages(
    query: str,
    evidence: tuple[SnapshotEvidence, ...],
    *,
    conflict_report: ConflictReport | None = None,
) -> list[dict[str, str]]:
    """Build the synthesis prompt. Carries ONLY the query + evidence grounding.

    Security-context fields are deliberately absent — the model must never see
    or produce tenant / identity / policy data (build plan §5.4). When a
    ``CONTRADICTED`` report is present, the model is instructed to present BOTH
    conflicting sources with their applicable times and cite both ``evidence_id``s
    — never pick a single answer (E-021 conflict rule).
    """
    user = f"Question:\n{query}\n\nAuthorized evidence:\n{_evidence_block(evidence)}"
    if (
        conflict_report is not None
        and conflict_report.conflict_status == ConflictStatus.CONTRADICTED
    ):
        contradicts = []
        for finding in conflict_report.findings:
            if finding.resolvable:
                continue
            for src in finding.sources:
                ef = src.effective_from.isoformat() if src.effective_from else "unknown"
                et = src.effective_to.isoformat() if src.effective_to else "open-ended"
                contradicts.append(
                    f"- evidence {src.evidence_id} (doc {src.document_id} v{src.document_version}, "
                    f"effective {ef} → {et})"
                )
        if contradicts:
            user += (
                "\n\nCONFLICT DETECTED — the evidence contradicts itself. Present BOTH "
                "conflicting sources with their applicable effective times and cite BOTH "
                "evidence ids. Do NOT pick a single answer.\n" + "\n".join(contradicts)
            )
    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


def _contradicted_coverage() -> SufficiencyResult:
    """A resolver-forced coverage verdict (E-021 conflict rule, issue #2).

    The resolver only emits ``conflict_status``; it never judges sufficiency. When
    it reports ``CONTRADICTED`` the pipeline attaches this coverage so the envelope
    completeness becomes ``conflicted``. The resolver never invents any other
    sufficiency verdict.
    """
    return SufficiencyResult(
        overall_status="contradicted",
        should_abstain=False,
        fact_coverage=(),
    )


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
        metadata_store: MetadataStore | None = None,
        judge: Judge | None = None,
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
        self._metadata_store = metadata_store
        self._judge = judge

    @property
    def judge(self) -> Judge | None:
        """The default Judge configured on this service (may be ``None``).

        The API uses this so a ``POST /v1/chat`` with a ``run_id`` (but no
        ``resume`` flag) can run the checkpointed iteration loop through the
        default judge instead of the single-pass ``answer`` path (E-023 P1-1).
        """
        return self._judge

    def answer(
        self,
        query: str,
        ctx: SecurityContext,
        corpus_id: str,
        *,
        run_id: str | None = None,
    ) -> AnswerEnvelope:
        """Answer one query over one corpus via the one-pass Fast Path (E-014).

        Equivalent to ``answer_with_iteration(max_rounds=1, judge=None)``: the
        E-019/E-020 judge + loop are not engaged, so all E-014 behaviour
        (including the ``insufficient`` → abstain short-circuit) is preserved.
        The single-pass path is not checkpointed, so ``run_id`` is accepted and
        ignored here (use ``answer_with_iteration`` with a judge for checkpointing).
        """
        return self.answer_with_iteration(
            query, ctx, corpus_id, max_rounds=1, judge=None, run_id=run_id
        )

    def _run_conflict_stage(
        self,
        query: str,
        evidence: tuple[SnapshotEvidence, ...],
        *,
        scope: TemporalScope | None = None,
    ) -> tuple[ConflictReport, tuple[SnapshotEvidence, ...]]:
        """Run the E-021 temporal filter + conflict resolver on post-retrieval evidence.

        Parses the ``TemporalScope`` once (or reuses a caller-supplied ``scope``,
        which the iteration path uses so the query is never re-parsed per round),
        filters, then resolves. Returns the resolver report and the surviving
        (kept) evidence in filter order — exactly the set the synthesis step
        should consume. The resolver only ever sees the already-authorized
        evidence it is handed (invariant 1).
        """
        if scope is None:
            scope = parse_temporal_scope(query)
        filt = filter_by_temporal_scope(evidence, scope)
        report = ConflictResolver().resolve(
            filt.retained, scope, topic_key=normalize_topic_key(query)
        )
        kept = set(report.resolved_evidence_ids)
        final_evidence = tuple(ev for ev in filt.retained if ev.evidence_id in kept)
        return report, final_evidence

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

        # 3) Single-pass synthesis from the merged, verified claims — but only if
        #    the temporal filter kept at least one evidence. A fully-filtered
        #    result (all expired / not-yet-effective / outside the window) must
        #    refuse without invoking the model (P1-1).
        report, final_evidence = self._run_conflict_stage(query, result.evidence)
        if not final_evidence:
            return build_no_evidence_refusal(
                ctx,
                corpora_used=result.corpora_used,
                tool_calls=result.retrieval_calls,
            )
        coverage = None
        if report.conflict_status == ConflictStatus.CONTRADICTED:
            coverage = _contradicted_coverage()
        return self._synthesize_multi_corpus(
            query,
            ctx,
            result,
            partial_retrieval=partial_retrieval,
            limitations=limitations,
            evidence=final_evidence,
            coverage=coverage,
            conflict_report=report,
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
        evidence: tuple[SnapshotEvidence, ...] | None = None,
        coverage: SufficiencyResult | None = None,
        conflict_report: ConflictReport | None = None,
    ) -> AnswerEnvelope:
        """Run LLM claim extraction over merged evidence, then build the envelope."""
        synthesis_evidence = evidence if evidence is not None else result.evidence
        messages = _build_messages(query, synthesis_evidence, conflict_report=conflict_report)
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
            evidence=synthesis_evidence,
            corpora_used=result.corpora_used,
            answer_markdown=extraction.draft_answer,
            claims=list(extraction.claims),
            coverage=coverage,
            tool_calls=result.retrieval_calls,
            limitations=limitations,
            partial_retrieval=partial_retrieval,
            stop_reason="evidence_found",
            conflict_report=conflict_report,
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
        run_id: str | None = None,
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

        When ``run_id`` is given, a :class:`RunCheckpoint` is persisted after each
        completed round (and on completion) so a crashed run can be resumed by
        ``resume_run`` (E-023). The single-pass ``answer`` path is not checkpointed.

        Args:
            query: The user question.
            ctx: The runtime-injected security context.
            corpus_id: The corpus to answer over (single-corpus in M3).
            max_rounds: Inclusive cap on rounds performed (default 3).
            judge: Optional pluggable ``Judge`` (the deterministic one for the
                Internal MVP). When ``None`` the loop is not engaged.
            required_facts: Explicit Required Facts; when omitted they are derived
                heuristically from the query.
            run_id: Optional stable id; when set, the run is checkpointed for
                later resume (E-023).
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

        # Parse the TemporalScope ONCE for the whole request (P1-2); reused every
        # round instead of re-parsing the query.
        scope = parse_temporal_scope(query)
        state = self._build_initial_state(query, first_result, corpus.corpus_id, scope)

        # Persist a checkpoint before the loop (round 0 already retrieved).
        if run_id is not None:
            self._save_checkpoint(
                run_id,
                ctx,
                state,
                first_result=first_result,
                required=required,
                max_rounds=max_rounds,
                query=query,
                corpus_id=corpus.corpus_id,
                round_index=0,
            )

        for round_idx in range(max_rounds):
            try:
                terminal = self._run_round(
                    state,
                    round_idx,
                    query,
                    ctx,
                    corpus,
                    first_result,
                    judge,
                    required,
                    gap_planner,
                    stop_policy,
                    max_rounds,
                    corpus.corpus_id,
                )
            except _BreakLoop:
                # A stop condition fired (all_sources_exhausted / conflict /
                # StopPolicy.should_stop). Persist the final checkpoint at the
                # next round boundary, mark the run done, and synthesize.
                if run_id is not None and self._metadata_store is not None:
                    self._save_checkpoint(
                        run_id,
                        ctx,
                        state,
                        first_result=first_result,
                        required=required,
                        max_rounds=max_rounds,
                        query=query,
                        corpus_id=corpus.corpus_id,
                        round_index=round_idx + 1,
                    )
                    self._metadata_store.mark_run_checkpoint_done(run_id)
                return self._finalize_iteration(query, ctx, first_result, state, verifier)
            if run_id is not None:
                self._save_checkpoint(
                    run_id,
                    ctx,
                    state,
                    first_result=first_result,
                    required=required,
                    max_rounds=max_rounds,
                    query=query,
                    corpus_id=corpus.corpus_id,
                    round_index=round_idx + 1,
                )
            if terminal is not None:
                if run_id is not None and self._metadata_store is not None:
                    self._metadata_store.mark_run_checkpoint_done(run_id)
                return terminal

        result = self._finalize_iteration(query, ctx, first_result, state, verifier)
        if run_id is not None and self._metadata_store is not None:
            self._metadata_store.mark_run_checkpoint_done(run_id)
        return result

    # ------------------------------------------------------------------ #
    # E-023: checkpoint + resume primitives
    # ------------------------------------------------------------------ #
    def _build_initial_state(
        self,
        query: str,
        first_result: FastPathResult,
        corpus_id: str,
        scope: "TemporalScope",
    ) -> _IterationState:
        """Build the initial loop working state from the round-0 fast path."""
        evidence_by_id = {ev.evidence_id: ev for ev in first_result.evidence}
        seen_ids = set(evidence_by_id)
        seen_text_hashes = {ev.text_hash for ev in first_result.evidence}
        seen_doc_versions = {(ev.document_id, ev.document_version) for ev in first_result.evidence}
        return _IterationState(
            evidence_by_id=evidence_by_id,
            seen_ids=seen_ids,
            seen_text_hashes=seen_text_hashes,
            seen_doc_versions=seen_doc_versions,
            prior_queries=[query],
            coverage=None,
            gap_rounds=0,
            retrieval_calls=1,  # round 0 counts as one retrieval pass
            prev_covered=set(),
            final_reason=first_result.stop_reason.value,  # P2-1 real termination reason
            scope=scope,
            final_report=None,
            final_evidence=tuple(evidence_by_id.values()),
            conflict_stop=False,
        )

    def _build_state_from_checkpoint(
        self, ck: RunCheckpoint, surviving: list[SnapshotEvidence]
    ) -> _IterationState:
        """Rebuild loop working state from a checkpoint over RE-AUTHORIZED evidence.

        Only ``surviving`` (still-authorized) evidence is carried forward; the
        trackers are rebuilt from it. ``final_evidence`` is re-seeded from the
        checkpoint's last conflict-stage ids so a completed run can be finalized
        without re-running rounds (E-023 re-auth: dropped evidence never returns).
        """
        evidence_by_id = {ev.evidence_id: ev for ev in surviving}
        seen_ids = set(evidence_by_id)
        seen_text_hashes = {ev.text_hash for ev in surviving}
        seen_doc_versions = {(ev.document_id, ev.document_version) for ev in surviving}
        coverage = ck.coverage
        # Distinguish "unresolved" (running / never set) from "explicitly empty"
        # (completed refusal).  A completed checkpoint with empty
        # final_evidence_ids means the terminal outcome had zero evidence
        # (P1-3 residual).
        if ck.status == CHECKPOINT_COMPLETED and not ck.final_evidence_ids:
            final_evidence: tuple[SnapshotEvidence, ...] = ()
        else:
            fe_set = set(ck.final_evidence_ids)
            final_evidence = (
                tuple(ev for ev in surviving if ev.evidence_id in fe_set)
                if fe_set
                else tuple(evidence_by_id.values())
            )
        return _IterationState(
            evidence_by_id=evidence_by_id,
            seen_ids=seen_ids,
            seen_text_hashes=seen_text_hashes,
            seen_doc_versions=seen_doc_versions,
            prior_queries=list(ck.prior_queries),
            coverage=coverage,
            gap_rounds=ck.gap_rounds,
            retrieval_calls=ck.retrieval_calls,
            prev_covered=set(coverage.covered_fact_ids) if coverage else set(),
            final_reason=ck.final_reason,
            scope=parse_temporal_scope(ck.query),
            final_report=ck.final_report,
            final_evidence=final_evidence,
            conflict_stop=ck.conflict_stop,
        )

    def _run_round(
        self,
        state: _IterationState,
        round_idx: int,
        query: str,
        ctx: SecurityContext,
        corpus: CorpusConfig,
        first_result: FastPathResult,
        judge: Judge,
        required: list[RequiredFact],
        gap_planner: GapPlanner,
        stop_policy: StopPolicy,
        max_rounds: int,
        corpus_id: str,
    ) -> AnswerEnvelope | None:
        """Execute one iteration round.

        Returns an :class:`AnswerEnvelope` only when the round is *terminal*
        (insufficient / no-evidence / judge fault) — the caller returns it
        immediately. Otherwise returns ``None`` (the caller persists the
        checkpoint and continues, or synthesizes after the loop).
        """
        state.gap_rounds = round_idx + 1

        if round_idx == 0:
            # Already retrieved via run_fast_path; everything is "new" this round.
            new_evidence_ids: set[str] = set(state.seen_ids)
            round_new_content = False
        else:
            # Coverage is always set by round 0's judge call above.
            assert state.coverage is not None
            plan = gap_planner.plan(
                state.coverage, prior_queries=state.prior_queries, corpus_id=corpus_id
            )
            # Only the candidate queries not yet executed this loop survive.
            pending = [q for q in plan.queries if q not in state.prior_queries]
            if not pending:
                # Nothing left to retrieve; this round did no work, so it must NOT
                # be counted as an executed round (P1-2).
                state.final_reason = "all_sources_exhausted"
                state.gap_rounds = round_idx
                raise _BreakLoop()
            # Execute exactly ONE new gap query this round (build plan §14.4/§14.5
            # spirit). Running a single query per round lets two *distinct*
            # gainless retrievals span two rounds and reach the `no_new_evidence`
            # stop (P1-1) instead of being collapsed into one round.
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
            state.retrieval_calls += 1
            for ev in evs:
                # Fail-closed: a gap snapshot from another tenant/corpus must
                # never enter the answer (P1-1). Mirrors the E-013 cross-tenant
                # guard that the M3 accumulation had regressed.
                if ev.tenant_id != ctx.tenant_id or ev.corpus_id != corpus.corpus_id:
                    raise TenantBindingError(
                        f"gap evidence {ev.evidence_id!r} tenant/corpus "
                        f"({ev.tenant_id}/{ev.corpus_id}) does not match request "
                        f"({ctx.tenant_id}/{corpus.corpus_id})"
                    )
                is_new_content = (
                    ev.text_hash not in state.seen_text_hashes
                    or (ev.document_id, ev.document_version) not in state.seen_doc_versions
                )
                existing = state.evidence_by_id.get(ev.evidence_id)
                if existing is None:
                    state.seen_ids.add(ev.evidence_id)
                    state.evidence_by_id[ev.evidence_id] = ev
                    # P2-2: a brand-new id only counts as a *gain* when it actually
                    # carries new information; a re-stated snapshot under a fresh id
                    # must NOT reset the no-gain counter.
                    if is_new_content:
                        new_evidence_ids.add(ev.evidence_id)
                elif (
                    existing.document_version != ev.document_version
                    or existing.text_hash != ev.text_hash
                ):
                    # Same id but an updated snapshot: keep the latest version so a
                    # new document version / new text is reflected (§14.6).
                    state.evidence_by_id[ev.evidence_id] = ev
                # §14.6 novelty: a new document version or new text hash is a genuine
                # gain even when the id was already seen (P2-2).
                if is_new_content:
                    round_new_content = True
                state.seen_text_hashes.add(ev.text_hash)
                state.seen_doc_versions.add((ev.document_id, ev.document_version))
            if q not in state.prior_queries:
                state.prior_queries.append(q)

        # --- E-021 conflict stage (P1-2): run BEFORE the Judge each round ---
        current_evidence = tuple(state.evidence_by_id.values())
        report, kept_evidence = self._run_conflict_stage(query, current_evidence, scope=state.scope)
        if not kept_evidence:
            # Temporal filter dropped every surviving evidence (all expired /
            # not-yet-effective / outside the window). Refuse without the model (P1-1).
            # Sync the terminal state so a persisted checkpoint can be resumed
            # idempotently without re-entering the loop (P1-3 residual).
            state.final_evidence = ()
            state.final_report = report
            state.coverage = SufficiencyResult(
                overall_status="insufficient",
                should_abstain=True,
                fact_coverage=(),
            )
            state.final_reason = "no_evidence"
            state.conflict_stop = False
            return build_no_evidence_refusal(
                ctx,
                corpora_used=(corpus.corpus_id,),
                tool_calls=state.retrieval_calls,
                gap_rounds=state.gap_rounds,
                iterations=state.gap_rounds,
            )
        if report.conflict_status == ConflictStatus.CONTRADICTED:
            # A contradiction cannot be auto-resolved: stop iterating and surface
            # both sources; do not run further gap retrieval (P1-2).
            state.final_report = report
            state.final_evidence = kept_evidence
            state.coverage = _contradicted_coverage()
            state.conflict_stop = True
            state.final_reason = "conflict_detected"
            raise _BreakLoop()
        # NONE / RESOLVED: the Coverage Judge only ever sees the resolved/retained
        # evidence (P1-2).
        state.final_report = report
        state.final_evidence = kept_evidence

        # Stage A: judge coverage over the conflict-filtered evidence.
        state.prev_covered = set(state.coverage.covered_fact_ids) if state.coverage else set()
        try:
            state.coverage = judge.judge(
                query=query, required_facts=required, evidence=kept_evidence
            )
        except (JudgeTimeoutError, JudgeError) as exc:
            # Judge fault: degrade conservatively (abstain), never fabricate.
            logger.warning("coverage judge failed; degrading conservatively: %s", exc)
            cov = SufficiencyResult(
                overall_status="insufficient",
                should_abstain=True,
                fact_coverage=(),
            )
            state.coverage = cov
            state.final_evidence = kept_evidence
            state.final_report = report
            state.final_reason = "judge_fault"
            state.conflict_stop = False
            return conservative_refusal(
                first_result,
                ctx,
                coverage=cov,
                gap_rounds=state.gap_rounds,
                iterations=state.gap_rounds,
                tool_calls=state.retrieval_calls,
            )

        new_covered = set(state.coverage.covered_fact_ids) - state.prev_covered

        decision = stop_policy.decide(
            round=round_idx,
            max_rounds=max_rounds,
            overall_status=state.coverage.overall_status,
            can_continue=state.coverage.can_continue_retrieval,
            new_evidence_ids=new_evidence_ids,
            new_covered_fact_ids=new_covered,
            judge_ok=True,
            budget_remaining=1.0,
            new_content=round_new_content,
        )
        state.final_reason = decision.reason  # surface the real stop reason (P2-1)
        if decision.should_stop:
            raise _BreakLoop()
        return None

    def _save_checkpoint(
        self,
        run_id: str,
        ctx: SecurityContext,
        state: _IterationState,
        *,
        first_result: FastPathResult,
        required: list[RequiredFact],
        max_rounds: int,
        query: str,
        corpus_id: str,
        round_index: int,
    ) -> None:
        """Persist the current loop state as a ``run_checkpoints`` row (E-023)."""
        if self._metadata_store is None:
            return
        ck = RunCheckpoint(
            run_id=run_id,
            tenant_id=ctx.tenant_id,
            user_id=ctx.user_id,
            session_id=ctx.session_id,
            policy_version=ctx.policy_version,
            query=query,
            corpus_id=corpus_id,
            max_rounds=max_rounds,
            required_facts=list(required),
            round_index=round_index,
            evidence=tuple(state.evidence_by_id.values()),
            prior_queries=list(state.prior_queries),
            seen_text_hashes=list(state.seen_text_hashes),
            seen_doc_versions=list(state.seen_doc_versions),
            retrieval_calls=state.retrieval_calls,
            gap_rounds=state.gap_rounds,
            final_reason=state.final_reason,
            conflict_stop=state.conflict_stop,
            coverage=state.coverage,
            final_report=state.final_report,
            final_evidence_ids=[ev.evidence_id for ev in state.final_evidence],
            first_result=first_result,
        )
        self._metadata_store.save_run_checkpoint(ck)

    def _finalize_iteration(
        self,
        query: str,
        ctx: SecurityContext,
        first_result: FastPathResult,
        state: _IterationState,
        verifier: DeterministicClaimEvidenceVerifier,
    ) -> AnswerEnvelope:
        """Synthesize the final envelope from the loop's terminal state."""
        coverage2 = state.coverage
        if (
            state.conflict_stop
            and state.final_report is not None
            and state.final_report.conflict_status == ConflictStatus.CONTRADICTED
        ):
            coverage2 = _contradicted_coverage()
        return self._synthesize(
            query,
            ctx,
            first_result,
            coverage=coverage2,
            verifier=verifier,
            evidence=state.final_evidence,
            gap_rounds=state.gap_rounds,
            iterations=state.gap_rounds,
            tool_calls=state.retrieval_calls,
            stop_reason=state.final_reason,
            conflict_report=state.final_report,
        )

    def resume_run(
        self,
        run_id: str,
        ctx: SecurityContext,
        *,
        judge: Judge | None = None,
    ) -> AnswerEnvelope:
        """Resume a checkpointed iteration loop, re-authorizing against CURRENT state.

        The security-critical E-023 guarantee: a resumed run is re-checked before
        any further retrieval/synthesis. Fail-closed — any of the following aborts
        the resume (raises :class:`ResumeAuthError`, mapped to a generic 5xx by the
        API) or drops evidence (never leaks data the principal can no longer read):

        * missing / aborted / foreign checkpoint (``run_id`` not found, or a
          different ``tenant_id`` / ``user_id`` / ``session_id``);
        * ``policy_version`` changed since the checkpoint was written (the stored
          authorization basis is stale);
        * the corpus is no longer discoverable for ``ctx``;
        * any gathered Evidence whose document is deleted / deprecated / re-versioned
          or whose current ACL denies ``ctx`` (build plan §3623 — "ACL 收紧不因旧
          Cache/Checkpoint 泄露"). Dropped evidence is recorded as a
          ``resume_evidence_revoked`` control-plane finding.

        Given identical re-auth outcomes, the resumed answer equals an uninterrupted
        run (deterministic).
        """
        if self._metadata_store is None:
            raise ResumeAuthError("metadata_store_unavailable")
        active_judge = judge or self._judge
        if active_judge is None:
            raise ResumeAuthError("judge_required")
        ck = self._metadata_store.load_run_checkpoint(run_id)
        if ck is None:
            raise ResumeAuthError("checkpoint_not_found")
        # A resumable checkpoint must have first_result (round 0 completed);
        # mypy uses this to narrow ck.first_result to FastPathResult below.
        assert ck.first_result is not None, "resumable checkpoint has no first_result"

        # 0) Persisted lifecycle status gate (E-023 P1-3). A checkpoint must be
        #    re-readable by its status, not just its presence:
        #      * aborted  → refuse resume (the run was explicitly cancelled);
        #      * running / completed → may be resumed (running continues,
        #        completed is re-authorized then returned idempotently).
        if ck.status == CHECKPOINT_ABORTED:
            raise ResumeAuthError("checkpoint_aborted")

        # 1) Cross-principal resume is refused (fail closed).
        if (
            ck.tenant_id != ctx.tenant_id
            or ck.user_id != ctx.user_id
            or ck.session_id != ctx.session_id
        ):
            raise ResumeAuthError("principal_mismatch")

        # 2) The authorization basis must be current (stale policy → abort).
        if ck.policy_version != ctx.policy_version:
            raise ResumeAuthError("policy_version_changed")

        # 3) Corpus discoverability re-check (whole-run gate).
        if self._registry is None:
            raise ResumeAuthError("registry_unavailable")
        try:
            self._registry.get(ck.corpus_id, ctx)
        except Exception:  # noqa: BLE001 - any discoverability failure aborts
            raise ResumeAuthError("corpus_not_discoverable")

        # 4) Re-authorize each gathered Evidence against CURRENT metadata.
        surviving: list[SnapshotEvidence] = []
        dropped: list[tuple[SnapshotEvidence, str]] = []
        for ev in ck.evidence:
            kept, reason = reauthorize_evidence(
                ev, ctx, metadata_store=self._metadata_store, registry=self._registry
            )
            if kept:
                surviving.append(ev)
            else:
                # Fail closed: drop the now-unauthorized evidence and record it for
                # audit. The dropped evidence must never re-surface (invariant 1).
                # The audit **detail** is a fixed, controlled string: it must NOT
                # repeat the evidence id or its body, which would re-leak data the
                # principal can no longer read (E-023 audit tightening; P1-6).
                self._metadata_store.record_control_plane_finding(
                    corpus_id=ev.corpus_id,
                    kind="resume_evidence_revoked",
                    tenant_id=ev.tenant_id,
                    document_id=ev.document_id,
                    document_version=ev.document_version,
                    detail=(
                        "checkpoint evidence revoked during current-policy "
                        f"reauthorization: {reason}"
                    ),
                )
                dropped.append((ev, reason))
        if not surviving:
            # No authorized evidence remains → refuse without invoking the model.
            # Save a terminal state and mark completed so resume is idempotent and
            # never re-enters the loop (P1-3 residual: completed no-evidence
            # checkpoint would otherwise assert state.coverage is not None on
            # round_idx > 0).
            if self._metadata_store is not None:
                state = self._build_state_from_checkpoint(ck, [])
                state.final_evidence = ()
                state.final_report = None
                state.coverage = SufficiencyResult(
                    overall_status="insufficient",
                    should_abstain=True,
                    fact_coverage=(),
                )
                state.final_reason = "all_evidence_revoked"
                state.conflict_stop = False
                self._save_checkpoint(
                    run_id,
                    ctx,
                    state,
                    first_result=ck.first_result,
                    required=ck.required_facts,
                    max_rounds=ck.max_rounds,
                    query=ck.query,
                    corpus_id=ck.corpus_id,
                    round_index=ck.round_index,
                )
                self._metadata_store.mark_run_checkpoint_done(run_id)
            return build_no_evidence_refusal(ctx, corpora_used=(ck.corpus_id,))

        corpus = self._resolve_corpus(ck.corpus_id)
        stop_policy = StopPolicy()
        gap_planner = GapPlanner()
        verifier = DeterministicClaimEvidenceVerifier()
        state = self._build_state_from_checkpoint(ck, surviving)

        # 5) Derived-state recomputation (E-023 P1-4).
        #
        # If NO evidence was revoked, the stored coverage / final_report /
        # final_evidence_ids are still valid: a completed checkpoint is finalized
        # idempotently (determinism, invariant 4). If AT LEAST ONE evidence was
        # revoked, the stored derived state is stale and MUST be recomputed from
        # the surviving evidence — re-run the temporal/conflict stage and the
        # coverage Judge. Reusing the old ``sufficient`` verdict would silently
        # keep answering from revoked data, and the old ConflictReport could still
        # name the revoked source. The recomputed verdict then decides whether to
        # stop or to continue retrieving.
        if dropped:
            # A completed checkpoint whose evidence was revoked must be explicitly
            # flipped to 'running' AND its derived state cleared before recompute
            # (P1-3 residual). ``_save_checkpoint`` preserves the existing status,
            # so we use ``mark_run_checkpoint_running`` instead.
            if ck.status == CHECKPOINT_COMPLETED and self._metadata_store is not None:
                self._metadata_store.mark_run_checkpoint_running(run_id)
                state.coverage = None
                state.final_report = None
                state.final_evidence = ()
                state.conflict_stop = False
                state.final_reason = None
            report, kept_evidence = self._run_conflict_stage(ck.query, tuple(surviving))
            if not kept_evidence:
                # Temporal filter dropped every surviving evidence → refuse.
                # Persist terminal state and mark completed (P1-3 residual).
                state.final_evidence = ()
                state.final_report = report
                state.coverage = SufficiencyResult(
                    overall_status="insufficient",
                    should_abstain=True,
                    fact_coverage=(),
                )
                state.final_reason = "temporal_no_evidence"
                state.conflict_stop = False
                self._save_checkpoint(
                    run_id,
                    ctx,
                    state,
                    first_result=ck.first_result,
                    required=ck.required_facts,
                    max_rounds=ck.max_rounds,
                    query=ck.query,
                    corpus_id=ck.corpus_id,
                    round_index=ck.round_index,
                )
                self._metadata_store.mark_run_checkpoint_done(run_id)
                return build_no_evidence_refusal(
                    ctx,
                    corpora_used=(ck.corpus_id,),
                    tool_calls=state.retrieval_calls,
                    gap_rounds=state.gap_rounds,
                    iterations=state.gap_rounds,
                )
            # Clear and recompute the derived state from surviving evidence only.
            state.final_report = report
            state.final_evidence = kept_evidence
            state.conflict_stop = report.conflict_status == ConflictStatus.CONTRADICTED
            state.coverage = active_judge.judge(
                query=ck.query, required_facts=ck.required_facts, evidence=kept_evidence
            )
            if state.conflict_stop:
                # A contradiction cannot be auto-resolved: surface both sources.
                state.coverage = _contradicted_coverage()
                self._metadata_store.mark_run_checkpoint_done(run_id)
                return self._finalize_iteration(ck.query, ctx, ck.first_result, state, verifier)
            if state.coverage.overall_status == "sufficient":
                # The reauthorized answer is sufficient from surviving evidence.
                self._metadata_store.mark_run_checkpoint_done(run_id)
                return self._finalize_iteration(ck.query, ctx, ck.first_result, state, verifier)
            # Otherwise: insufficient → continue iterating from ck.round_index.
        elif ck.status == CHECKPOINT_COMPLETED and not dropped:
            # No evidence revoked and the checkpoint was already completed →
            # idempotent return (invariant 4).  For refusal outcomes (empty final
            # evidence) return deterministically WITHOUT calling Retriever / Judge
            # / Model — the stored state is authoritative (P1-3 residual).
            if not state.final_evidence:
                return build_no_evidence_refusal(
                    ctx,
                    corpora_used=(ck.corpus_id,),
                    tool_calls=state.retrieval_calls,
                    gap_rounds=state.gap_rounds,
                    iterations=state.gap_rounds,
                )
            self._metadata_store.mark_run_checkpoint_done(run_id)
            return self._finalize_iteration(ck.query, ctx, ck.first_result, state, verifier)

        # 6) Continue the loop from the checkpointed round. Every completed round
        #    is persisted (E-023 P1-3) so a second crash resumes from the latest
        #    round, not the stale one — and the run ends only after the final
        #    state has been written (terminal / BreakLoop → save THEN mark done).
        for round_idx in range(ck.round_index, ck.max_rounds):
            try:
                terminal = self._run_round(
                    state,
                    round_idx,
                    ck.query,
                    ctx,
                    corpus,
                    ck.first_result,
                    active_judge,
                    ck.required_facts,
                    gap_planner,
                    stop_policy,
                    ck.max_rounds,
                    ck.corpus_id,
                )
            except _BreakLoop:
                # Persist the latest state BEFORE marking done (P1-3), then stop.
                if self._metadata_store is not None:
                    self._save_checkpoint(
                        run_id,
                        ctx,
                        state,
                        first_result=ck.first_result,
                        required=ck.required_facts,
                        max_rounds=ck.max_rounds,
                        query=ck.query,
                        corpus_id=ck.corpus_id,
                        round_index=round_idx + 1,
                    )
                    self._metadata_store.mark_run_checkpoint_done(run_id)
                return self._finalize_iteration(ck.query, ctx, ck.first_result, state, verifier)
            # P1-3: persist after each completed (non-terminal) round.
            if self._metadata_store is not None:
                self._save_checkpoint(
                    run_id,
                    ctx,
                    state,
                    first_result=ck.first_result,
                    required=ck.required_facts,
                    max_rounds=ck.max_rounds,
                    query=ck.query,
                    corpus_id=ck.corpus_id,
                    round_index=round_idx + 1,
                )
            if terminal is not None:
                self._metadata_store.mark_run_checkpoint_done(run_id)
                return terminal

        result = self._finalize_iteration(ck.query, ctx, ck.first_result, state, verifier)
        if self._metadata_store is not None:
            self._metadata_store.mark_run_checkpoint_done(run_id)
        return result

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

        report, final_evidence = self._run_conflict_stage(query, result.evidence)
        if not final_evidence:
            # Temporal filter dropped every retrieved evidence (all expired /
            # not-yet-effective / outside the historical window). Refuse without
            # invoking the model (P1-1).
            return build_no_evidence_refusal(ctx, corpora_used=(result.corpus_id,))
        coverage = None
        if report.conflict_status == ConflictStatus.CONTRADICTED:
            coverage = _contradicted_coverage()
        return self._synthesize(
            query,
            ctx,
            result,
            evidence=final_evidence,
            coverage=coverage,
            conflict_report=report,
        )

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
        conflict_report: ConflictReport | None = None,
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
        messages = _build_messages(query, synthesis_evidence, conflict_report=conflict_report)
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
            conflict_report=conflict_report,
        )
