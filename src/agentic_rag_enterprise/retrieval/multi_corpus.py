"""E-016 cross-corpus retrieval, merge & dedup (build plan §9.5 / Milestone 4).

Runs the *existing* single-corpus ``SecureRetriever.retrieve_evidence`` once per
selected corpus, passing the same ``SecurityContext`` so every per-corpus
tenant / ACL / active-version / parent-second-auth constraint still applies. The
results are merged into one deterministic, deduplicated Evidence set.

Fault handling (fail-loud, never fail-silent, and never *fail-open*):

* Only *explicit backend / infrastructure* faults are captured as a
  :class:`CorpusRetrievalFault`; the other corpora's evidence is still returned.
  A backend fault is never relabelled as "no Evidence".
* **Security, tenant-binding, corpus-binding, authorization and configuration
  errors propagate immediately** (fail closed). They are never downgraded to a
  partial retrieval fault even when another corpus returns evidence — a denial or
  a binding violation must never be masked by a successful sibling corpus.
* ``retrieve`` raises only when *every* selected corpus faults (a total outage is
  an error, not an abstain). A partial fault is surfaced in ``faults`` for the
  caller to degrade with an explicit limitation.
* Every returned Evidence is re-bound to the requested tenant and the corpus it
  was requested from; a snapshot that claims a different tenant/corpus is a
  security violation and raises (never a fault, never merged).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from agentic_rag_enterprise.answer.envelope import TenantBindingError
from agentic_rag_enterprise.domain.corpus import CorpusConfig
from agentic_rag_enterprise.domain.evidence import Evidence as SnapshotEvidence
from agentic_rag_enterprise.domain.security import SecurityContext
from agentic_rag_enterprise.retrieval.models import (
    CorpusNotDiscoverableError,
    ParentAuthorizationError,
)
from agentic_rag_enterprise.retrieval.retriever import SecureRetriever
from agentic_rag_enterprise.security.filter import EmptyAuthorizationScopeError
from agentic_rag_enterprise.storage.vector_store import DenseEncoder, SparseEncoder

# Security / authorization / binding / configuration errors that MUST propagate
# (fail closed) rather than be captured as a partial backend fault. A denial from
# one corpus is never masked by a successful sibling corpus.
_PROPAGATE_ERRORS: tuple[type[Exception], ...] = (
    CorpusNotDiscoverableError,
    ParentAuthorizationError,
    EmptyAuthorizationScopeError,
    TenantBindingError,
)


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
    # Number of per-corpus retrieval calls actually executed (successful or faulted),
    # so the envelope can report a truthful ``tool_calls`` (P2-2).
    retrieval_calls: int


@dataclass
class _MergeState:
    """Mutable accumulator for ``merge_evidence`` (pure w.r.t. inputs order)."""

    survivors: list[SnapshotEvidence] = field(default_factory=list)
    # evidence_id -> index into ``survivors`` (first occurrence wins).
    id_keys: dict[str, int] = field(default_factory=dict)
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

    Two-layer dedup (build plan §9.5 / E-016 contract):

    1. **Stable ``evidence_id`` dedup — first occurrence wins.** A repeated
       ``evidence_id`` is dropped *before* any content folding, so a single
       ``evidence_id`` can never map to two non-interchangeable snapshots
       (different text/version/corpus). The first occurrence (by corpus_id asc,
       then input order) is authoritative.
    2. **Cross-id same-content folding.** Two Evidence with *different*
       ``evidence_id`` but the same ``(text_hash, document_id, document_version)``
       collapse to the higher ``authority_level`` (tie → keep the existing
       survivor). The loser's ``corpus_id`` is still marked as contributed (source
       attribution preserved) but only one primary Evidence is emitted. Same text
       under a *different* ``document_version`` is NOT folded (kept distinct).

    Returns survivors ordered deterministically (corpus_id asc, then input order).
    """
    state = _MergeState()
    for corpus_id in sorted(per_corpus):
        for ev in per_corpus[corpus_id]:
            state.contributed.add(ev.corpus_id)

            # Layer 1: stable evidence_id dedup (first occurrence wins).
            if ev.evidence_id in state.id_keys:
                continue

            # Layer 2: cross-id same-content folding.
            key = (ev.text_hash, ev.document_id, ev.document_version)
            existing_idx = state.text_keys.get(key)
            if existing_idx is None:
                idx = len(state.survivors)
                state.survivors.append(ev)
                state.id_keys[ev.evidence_id] = idx
                state.text_keys[key] = idx
                continue

            # Text collision under a different id: keep the higher authority
            # (tie → existing survivor). Record the id so a later exact repeat of
            # this loser id is still deduped, but do NOT emit a second primary.
            state.id_keys[ev.evidence_id] = existing_idx
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
        """Retrieve + merge Evidence across ``corpora`` with fail-loud fault handling.

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
            CorpusNotDiscoverableError / ParentAuthorizationError /
            EmptyAuthorizationScopeError / TenantBindingError: propagated
            immediately (fail closed) — a security / binding / authorization error
            is never downgraded to a partial fault, even if a sibling corpus
            succeeds.
            Exception: the original backend error, re-raised only when *every*
            selected corpus faults (a total outage is an error, never an abstain).
        """
        per_corpus: dict[str, list[SnapshotEvidence]] = {}
        faults: list[CorpusRetrievalFault] = []
        insufficient: list[str] = []
        last_exc: Exception | None = None
        calls = 0

        for corpus in corpora:
            calls += 1
            try:
                evs = self._retriever.retrieve_evidence(
                    ctx,
                    query,
                    corpus,
                    top_k,
                    dense_encoder=dense_encoder,
                    sparse_encoder=sparse_encoder,
                )
            except _PROPAGATE_ERRORS:
                # Security / binding / authorization / config error → fail closed.
                # Never masked by a successful sibling corpus.
                raise
            except Exception as exc:  # noqa: BLE001 - only backend faults reach here
                faults.append(
                    CorpusRetrievalFault(
                        corpus_id=corpus.corpus_id,
                        reason=f"retrieval failed for corpus {corpus.corpus_id!r}",
                        error_type=type(exc).__name__,
                    )
                )
                last_exc = exc
                continue

            # Cross-corpus evidence binding: a snapshot must belong to the tenant we
            # asked as and the corpus we asked from. A mismatch is a security
            # violation, not a backend fault — raise, never merge (P2-3).
            self._assert_evidence_binding(ctx, corpus, evs)

            if evs:
                per_corpus[corpus.corpus_id] = list(evs)
            else:
                insufficient.append(corpus.corpus_id)

        # Total failure only when EVERY selected corpus faulted (P1-4.1). A single
        # fault alongside a legitimately-empty sibling is NOT a total outage.
        if faults and len(faults) == len(corpora):
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
            retrieval_calls=calls,
        )

    @staticmethod
    def _assert_evidence_binding(
        ctx: SecurityContext,
        corpus: CorpusConfig,
        evidence: list[SnapshotEvidence] | tuple[SnapshotEvidence, ...],
    ) -> None:
        """Fail-closed: every returned snapshot must match the requested tenant/corpus."""
        for ev in evidence:
            if ev.tenant_id != ctx.tenant_id:
                raise TenantBindingError(
                    f"Evidence {ev.evidence_id!r} from corpus {corpus.corpus_id!r} "
                    f"belongs to tenant {ev.tenant_id!r}, not {ctx.tenant_id!r}"
                )
            if ev.corpus_id != corpus.corpus_id:
                raise TenantBindingError(
                    f"Evidence {ev.evidence_id!r} claims corpus {ev.corpus_id!r}, "
                    f"but was retrieved from corpus {corpus.corpus_id!r}"
                )
