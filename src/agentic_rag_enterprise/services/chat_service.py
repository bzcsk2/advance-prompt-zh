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
from agentic_rag_enterprise.answer.envelope import AnswerEnvelope
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
from agentic_rag_enterprise.retrieval.retriever import SecureRetriever
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
        top_k: int | None = None,
    ) -> None:
        self._retriever = retriever
        self._dense_encoder = dense_encoder
        self._sparse_encoder = sparse_encoder
        self._model = model
        self._resolve_corpus = resolve_corpus
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
        prior_queries = [query]
        coverage: SufficiencyResult | None = None
        gap_rounds = 0
        retrieval_calls = 1  # round 0 counts as one retrieval pass
        prev_covered: set[str] = set()

        for round_idx in range(max_rounds):
            gap_rounds = round_idx + 1

            if round_idx == 0:
                # Already retrieved via run_fast_path; everything is "new" this round.
                new_evidence_ids: set[str] = set(seen_ids)
                round_queries: list[str] = [query]
            else:
                # Coverage is always set by round 0's judge call above.
                assert coverage is not None
                plan = gap_planner.plan(coverage, prior_queries=prior_queries, corpus_id=corpus_id)
                round_queries = list(plan.queries)
                if not round_queries:
                    # No remaining gap queries → nothing left to retrieve for.
                    break
                new_evidence_ids = set()
                for q in round_queries:
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
                        if ev.evidence_id not in seen_ids:
                            seen_ids.add(ev.evidence_id)
                            evidence_by_id[ev.evidence_id] = ev
                            new_evidence_ids.add(ev.evidence_id)
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
            )
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
    ) -> AnswerEnvelope:
        """Run LLM claim extraction + Stage B verification, then build the envelope.

        The model prompt carries the (accumulated) evidence so claims can cite it.
        When ``coverage`` is present, Stage B (``DeterministicClaimEvidenceVerifier``)
        assigns each kept claim a ``support_status`` and the verdict is attached.
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
        )
