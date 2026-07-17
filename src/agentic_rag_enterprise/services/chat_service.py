"""E-014 shared chat application service (build plan §2.2 / §5 / §6).

One reusable service that backs BOTH the synchronous ``POST /v1/chat`` FastAPI
endpoint and the minimal Gradio adapter. It wires the already-built layers:

* **E-012** ``run_fast_path`` — the one-pass sufficient / insufficient decision
  (exactly one ``retrieve_evidence`` call);
* **E-011** ``Evidence`` snapshots — the immutable grounding + citation source;
* **E-013** ``build_answer_envelope`` / ``conservative_refusal`` — the typed,
  validated, fail-closed answer envelope.

The LLM is invoked ONLY here, and only to (a) extract atomic ``Claim``s each
bound to a real ``evidence_id`` and (b) produce a draft prose. Per E-013 the
draft is advisory: the final answer is always derived from the *verified* claims.
Security-context fields (tenant / user / policy / …) are NEVER sent to, or read
back from, the model — they are strictly runtime-injected (build plan §5.4).
"""

from __future__ import annotations

from typing import Callable, cast

from agentic_rag_enterprise.answer import build_answer_envelope, conservative_refusal
from agentic_rag_enterprise.answer.envelope import AnswerEnvelope
from agentic_rag_enterprise.domain.corpus import CorpusConfig
from agentic_rag_enterprise.domain.evidence import Evidence as SnapshotEvidence
from agentic_rag_enterprise.domain.security import SecurityContext
from agentic_rag_enterprise.providers import ModelProvider
from agentic_rag_enterprise.retrieval.fast_path import (
    FastPathBackendError,
    FastPathSufficiency,
    run_fast_path,
)
from agentic_rag_enterprise.retrieval.retriever import SecureRetriever
from agentic_rag_enterprise.services.claims_schema import ClaimExtraction
from agentic_rag_enterprise.storage.vector_store import DenseEncoder, SparseEncoder


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
        """Answer one query over one corpus via the one-pass Fast Path.

        Propagates ``FastPathBackendError`` and model faults as typed errors; it
        never masks a retrieval/model fault as a grounded answer or a refusal.
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

        messages = _build_messages(query, result.evidence)
        try:
            extraction = cast(
                ClaimExtraction,
                self._model.with_structured_output(ClaimExtraction).invoke(messages),
            )
        except Exception as exc:  # noqa: BLE001 - wrapped as a typed service error
            raise ModelInvocationError(
                f"claim extraction failed for corpus {corpus_id!r}: {exc}"
            ) from exc

        return build_answer_envelope(
            result,
            ctx,
            answer_markdown=extraction.draft_answer,
            claims=list(extraction.claims),
        )
