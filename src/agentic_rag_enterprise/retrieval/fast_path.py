"""Single-corpus Fast Path and one-pass sufficiency decision (E-012).

The Fast Path is the thin M2 decision layer that sits on top of E-011's
``SecureRetriever.retrieve_evidence``. It performs **exactly one** retrieval,
then applies the deterministic baseline sufficiency rule from build plan
§14.3 / §14.7:

* at least one ``Evidence`` snapshot returned → ``sufficient``;
* zero ``Evidence`` returned → ``insufficient`` (the downstream answer phase
  must conservatively abstain / refuse).

It deliberately does NOT run a Planner DAG, does NOT issue a second retrieval,
and does NOT perform any LLM-based Required-Fact judging (those belong to
E-013 / E-019 / E-020). A retrieval/infrastructure fault is surfaced as a typed
``FastPathBackendError`` and is never silently relabelled as ``insufficient``
("no answer"), so the answer phase cannot mistake a backend outage for "the
corpus had nothing to say".

The result model is frozen and validated so its fields can never contradict
each other (e.g. an empty ``evidence`` list alongside ``sufficient``).
"""

from enum import Enum

from pydantic import BaseModel, ConfigDict, computed_field, model_validator

from agentic_rag_enterprise.domain.corpus import CorpusConfig
from agentic_rag_enterprise.domain.evidence import Evidence as SnapshotEvidence
from agentic_rag_enterprise.domain.security import SecurityContext
from agentic_rag_enterprise.retrieval.retriever import SecureRetriever
from agentic_rag_enterprise.storage.vector_store import DenseEncoder, SparseEncoder


class FastPathSufficiency(str, Enum):
    """Deterministic baseline sufficiency verdict (build plan §14.3).

    Only the two states the one-pass Fast Path can produce. The richer
    ``partially_sufficient`` / ``contradicted`` / ``ambiguous`` / ``policy_blocked``
    vocabulary belongs to the E-019/E-020 Required-Fact judge and is intentionally
    out of scope here.
    """

    SUFFICIENT = "sufficient"
    INSUFFICIENT = "insufficient"


class FastPathStopReason(str, Enum):
    """Why the one-pass Fast Path stopped (build plan §14.5 / §14.7).

    A retrieval/infra fault is NOT a stop reason — it raises
    :class:`FastPathBackendError` instead, so it can never be confused with a
    deliberate ``no_evidence`` decision.
    """

    EVIDENCE_FOUND = "evidence_found"
    NO_EVIDENCE = "no_evidence"


class FastPathResult(BaseModel):
    """Typed, immutable output of the single-corpus Fast Path.

    The model is frozen and validated so the fields can never contradict one
    another (build plan §14.7). ``evidence`` is a tuple (not a list) and the
    ``is_sufficient`` / ``should_abstain`` booleans are computed from
    ``sufficiency`` — they cannot be set to an inconsistent value.

    State combinations are locked by ``_lock_state``:
    * ``sufficient``  ⇒ non-empty ``evidence`` and ``stop_reason == evidence_found``;
    * ``insufficient`` ⇒ empty ``evidence`` and ``stop_reason == no_evidence``.
    """

    model_config = ConfigDict(frozen=True)

    query: str
    corpus_id: str
    tenant_id: str

    evidence: tuple[SnapshotEvidence, ...]
    sufficiency: FastPathSufficiency
    stop_reason: FastPathStopReason

    @computed_field
    def is_sufficient(self) -> bool:
        return self.sufficiency is FastPathSufficiency.SUFFICIENT

    @computed_field
    def should_abstain(self) -> bool:
        return self.sufficiency is FastPathSufficiency.INSUFFICIENT

    @model_validator(mode="after")
    def _lock_state(self) -> "FastPathResult":
        if self.sufficiency is FastPathSufficiency.SUFFICIENT:
            if not self.evidence:
                raise ValueError("sufficient result must carry at least one Evidence")
            if self.stop_reason is not FastPathStopReason.EVIDENCE_FOUND:
                raise ValueError("sufficient result stop_reason must be evidence_found")
        else:  # INSUFFICIENT
            if self.evidence:
                raise ValueError("insufficient result must carry no Evidence")
            if self.stop_reason is not FastPathStopReason.NO_EVIDENCE:
                raise ValueError("insufficient result stop_reason must be no_evidence")
        return self

    @classmethod
    def _build(
        cls,
        *,
        query: str,
        corpus_id: str,
        tenant_id: str,
        evidence: list[SnapshotEvidence],
        sufficiency: FastPathSufficiency,
        stop_reason: FastPathStopReason,
    ) -> "FastPathResult":
        return cls(
            query=query,
            corpus_id=corpus_id,
            tenant_id=tenant_id,
            evidence=tuple(evidence),
            sufficiency=sufficiency,
            stop_reason=stop_reason,
        )


class FastPathBackendError(Exception):
    """Typed error raised when the single ``retrieve_evidence`` dependency fails.

    The Fast Path never masks a retrieval/infra fault as an ``insufficient``
    (no-answer) decision. The original exception is preserved as ``__cause__``.
    """


def run_fast_path(
    retriever: SecureRetriever,
    ctx: SecurityContext,
    query: str,
    corpus: CorpusConfig,
    *,
    top_k: int | None = None,
    dense_encoder: DenseEncoder,
    sparse_encoder: SparseEncoder,
) -> FastPathResult:
    """Run the one-pass single-corpus Fast Path.

    Calls ``SecureRetriever.retrieve_evidence`` exactly once with the caller's
    ``SecurityContext`` and ``CorpusConfig`` passed through unchanged, then
    applies the deterministic baseline sufficiency rule. Any failure of the
    retrieval dependency propagates as :class:`FastPathBackendError`.

    Args:
        retriever: An already-constructed ``SecureRetriever`` (the E-011 secure
            retrieval boundary, which enforces corpus discoverability, parent
            second-authorization, deduplication, and snapshot persistence).
        ctx: The runtime-injected security context. Passed unchanged into the
            secure retrieval boundary.
        query: The user question. Forwarded verbatim to ``retrieve_evidence``.
        corpus: One already-authorized ``CorpusConfig``. Passed unchanged into
            the secure retrieval boundary (the corpus-discoverability gate runs
            inside ``retrieve_evidence``).
        top_k: Optional retrieval width (defaults to the retriever's setting).
        dense_encoder / sparse_encoder: The injected encoders required by the
            hybrid search adapter.

    Returns:
        A frozen :class:`FastPathResult` with the retrieved ``Evidence``, a
        ``sufficient`` / ``insufficient`` verdict, and the stop reason.

    Raises:
        FastPathBackendError: if the single ``retrieve_evidence`` call raises.
            The underlying exception is attached as ``__cause__``.
    """
    try:
        # Exactly one retrieval pass. No Planner, no second query, no loop.
        evidence = retriever.retrieve_evidence(
            ctx,
            query,
            corpus,
            top_k,
            dense_encoder=dense_encoder,
            sparse_encoder=sparse_encoder,
        )
    except Exception as exc:  # noqa: BLE001 - re-wrapped as a typed backend error
        raise FastPathBackendError(
            f"Fast Path retrieval dependency failed for corpus {corpus.corpus_id!r}: {exc}"
        ) from exc

    # Deterministic baseline sufficiency (build plan §14.3 / §14.7).
    if evidence:
        return FastPathResult._build(
            query=query,
            corpus_id=corpus.corpus_id,
            tenant_id=ctx.tenant_id,
            evidence=evidence,
            sufficiency=FastPathSufficiency.SUFFICIENT,
            stop_reason=FastPathStopReason.EVIDENCE_FOUND,
        )

    return FastPathResult._build(
        query=query,
        corpus_id=corpus.corpus_id,
        tenant_id=ctx.tenant_id,
        evidence=[],
        sufficiency=FastPathSufficiency.INSUFFICIENT,
        stop_reason=FastPathStopReason.NO_EVIDENCE,
    )
