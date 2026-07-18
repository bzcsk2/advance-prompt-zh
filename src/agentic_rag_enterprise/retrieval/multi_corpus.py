"""E-016 cross-corpus retrieval, merge & dedup (build plan §9.5 / Milestone 4).

Runs the *existing* single-corpus ``SecureRetriever.retrieve_evidence`` once per
selected corpus, passing the same ``SecurityContext`` so every per-corpus
tenant / ACL / active-version / parent-second-auth constraint still applies. The
results are merged into one deterministic, deduplicated Evidence set.

Fault handling is fail-loud, never fail-silent: a backend fault in one corpus is
captured as a :class:`CorpusRetrievalFault` and the other corpora's evidence is
still returned. Only when *every* selected corpus faults does ``retrieve`` raise —
a retrieval outage is not an answer.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from agentic_rag_enterprise.domain.corpus import CorpusConfig
from agentic_rag_enterprise.domain.evidence import Evidence as SnapshotEvidence
from agentic_rag_enterprise.domain.security import SecurityContext
from agentic_rag_enterprise.retrieval.retriever import SecureRetriever
from agentic_rag_enterprise.storage.vector_store import DenseEncoder, SparseEncoder


@dataclass(frozen=True)
class CorpusRetrievalFault:
    """A backend fault for one corpus — never relabelled as "no Evidence"."""

    corpus_id: str
    reason: str
    error_type: str


@dataclass(frozen=True)
class MultiCorpusResult:
    """Merged, deduplicated cross-corpus retrieval outcome (deterministic order)."""

    evidence: tuple[SnapshotEvidence, ...]
    corpora_used: tuple[str, ...]
    routed: tuple[str, ...]
    faults: tuple[CorpusRetrievalFault, ...]
    insufficient_corpora: tuple[str, ...]


@dataclass
class _MergeState:
    """Mutable accumulator for ``merge_evidence`` (pure w.r.t. inputs order)."""

    survivors: list[SnapshotEvidence] = field(default_factory=list)
    # (text_hash, document_id, document_version) -> index into ``survivors`` of the
    # kept (higher-authority) survivor for that text group.
    text_keys: dict[tuple[str, str, str], int] = field(default_factory=dict)
    # corpus_id -> did it contribute (primary or folded) evidence?
    contributed: set[str] = field(default_factory=set)


def merge_evidence(
    per_corpus: dict[str, list[SnapshotEvidence]],
) -> tuple[SnapshotEvidence, ...]:
    """Merge + dedup Evidence from several corpora in a deterministic order.

    Iterates corpora in ascending ``corpus_id`` order, evidence in input order.

    Dedup rules:
    * by stable ``evidence_id`` (first occurrence wins);
    * cross-corpus same-content folding: two Evidence sharing
      ``(text_hash, document_id, document_version)`` but a *different*
      ``evidence_id`` collapse to the higher ``authority_level`` (tie → keep the
      existing survivor). The loser's ``corpus_id`` is still marked as contributed
      (source attribution preserved) but only one primary Evidence is emitted;
    * same text under a *different* ``document_version`` is NOT folded (kept
      distinct).

    Returns survivors ordered deterministically (corpus_id asc, then input order).
    """
    state = _MergeState()
    for corpus_id in sorted(per_corpus):
        for ev in per_corpus[corpus_id]:
            state.contributed.add(ev.corpus_id)
            key = (ev.text_hash, ev.document_id, ev.document_version)
            existing_idx = state.text_keys.get(key)
            if existing_idx is None:
                # No text collision yet: keep as a new primary.
                state.text_keys[key] = len(state.survivors)
                state.survivors.append(ev)
                continue
            # Text collision: keep the higher authority (tie → existing survivor).
            if ev.authority_level > state.survivors[existing_idx].authority_level:
                state.survivors[existing_idx] = ev
    return tuple(state.survivors)


class MultiCorpusRetrieval:
    """Run ``SecureRetriever.retrieve_evidence`` across selected corpora (E-016)."""

    def __init__(self, retriever: SecureRetriever) -> None:
        self._retriever = retriever

    def retrieve(
        self,
        ctx: SecurityContext,
        query: str,
        corpora: list[CorpusConfig],
        *,
        top_k: int | None = None,
        dense_encoder: DenseEncoder,
        sparse_encoder: SparseEncoder,
    ) -> MultiCorpusResult:
        """Retrieve + merge Evidence across ``corpora`` with fail-loud faults.

        Args:
            ctx: The runtime-injected security context; passed unchanged into every
                per-corpus ``retrieve_evidence`` call.
            query: The user question, forwarded verbatim to each corpus.
            corpora: The already-authorized, already-routed corpora to query.
            top_k: Optional per-corpus retrieval width.
            dense_encoder / sparse_encoder: Injected encoders for the hybrid adapter.

        Returns:
            A :class:`MultiCorpusResult`. Backend faults for individual corpora are
            captured in ``faults``; the remaining evidence is still merged and
            returned, and the faulted corpora are excluded from ``corpora_used``.

        Raises:
            Any exception if *every* selected corpus faults — a total retrieval
            outage must surface as an error, never as an abstain / "no evidence".
            The original exception of the *last* faulting corpus is re-raised.
        """
        per_corpus: dict[str, list[SnapshotEvidence]] = {}
        faults: list[CorpusRetrievalFault] = []
        insufficient: list[str] = []
        last_exc: Exception | None = None

        for corpus in corpora:
            try:
                evs = self._retriever.retrieve_evidence(
                    ctx,
                    query,
                    corpus,
                    top_k,
                    dense_encoder=dense_encoder,
                    sparse_encoder=sparse_encoder,
                )
            except Exception as exc:  # noqa: BLE001 - captured as an explicit fault
                faults.append(
                    CorpusRetrievalFault(
                        corpus_id=corpus.corpus_id,
                        reason=f"retrieval failed for corpus {corpus.corpus_id!r}",
                        error_type=type(exc).__name__,
                    )
                )
                last_exc = exc
                continue
            if evs:
                per_corpus[corpus.corpus_id] = list(evs)
            else:
                insufficient.append(corpus.corpus_id)

        if not per_corpus and faults:
            # Total failure: surface a backend outage, not an answer.
            assert last_exc is not None
            raise last_exc

        merged = merge_evidence(per_corpus)
        # corpora_used = every corpus that contributed (primary OR folded) evidence,
        # so cross-corpus same-text folding preserves source attribution.
        corpora_used = tuple(sorted(per_corpus))
        routed = tuple(c.corpus_id for c in corpora)
        return MultiCorpusResult(
            evidence=merged,
            corpora_used=corpora_used,
            routed=routed,
            faults=tuple(faults),
            insufficient_corpora=tuple(sorted(insufficient)),
        )
