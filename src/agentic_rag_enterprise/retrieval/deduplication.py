"""Retrieval-hit deduplication (build plan §12.4 / §12.6).

The secure retrieval pipeline returns authorized child hits that can overlap in
several ways. Before the parent second-authorization pass and before evidence
snapshotting, hits are deduplicated so the same underlying content is not
presented to the model (or persisted as evidence) multiple times.

Deduplication dimensions (build plan §12.6):

1. **Same document + version + span** — identical child chunk.
2. **Same parent hit by multiple children** — keep the highest-scoring child,
   merge the queries that produced the others.
3. **Near-duplicate / cross-corpus same content** — keep the higher-authority
   source (then higher score), record the collapsed (corpus, document, version,
   parent, chunk) sources.

Merging query sources preserves *which* retrieval iterations/queries surfaced a
hit, so the downstream evidence snapshot's provenance is complete even after a
hit is collapsed. The collapsed duplicates are recorded so an auditor can see
that the surviving evidence subsumed other retrievals.

The deduplicator is pure (no I/O): it operates on :class:`DedupCandidate`
records built by the caller from :class:`~agentic_rag_enterprise.retrieval.models.RetrievalHit`.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from typing import Any

from agentic_rag_enterprise.retrieval.models import RetrievalHit

# Whitespace / casing normalized for near-duplicate comparison.
_WS_RE = re.compile(r"\s+")


def normalize_text(text: str) -> str:
    """Lowercase + collapse whitespace; the canonical key for text dedup."""
    return _WS_RE.sub(" ", (text or "").strip().lower())


@dataclass
class RetrievalContext:
    """The retrieval that produced a hit: which query, iteration and plan step."""

    query: str
    iteration: int = 0
    plan_step_id: str | None = None


@dataclass
class DedupCandidate:
    """A retrieval hit plus the context and canonical text used for dedup.

    ``contexts`` accumulates every retrieval context that surfaced this (or an
    equivalent, collapsed) hit, in descending score order. ``duplicate_sources``
    records the provenance of every hit that was folded into this survivor.
    """

    hit: RetrievalHit
    contexts: list[RetrievalContext]
    text: str
    # Corpus/document authority used as the cross-corpus tie-breaker (§12.6 #4).
    authority_level: int = 50
    duplicate_sources: list[dict[str, Any]] = field(default_factory=list)

    @property
    def primary_context(self) -> RetrievalContext:
        return self.contexts[0]

    def _source_record(self) -> dict[str, Any]:
        h = self.hit
        return {
            "corpus_id": h.corpus_id,
            "document_id": h.document_id,
            "document_version": h.document_version,
            "parent_id": h.parent_id,
            "chunk_id": h.chunk_id,
            "authority_level": self.authority_level,
        }


class Deduplicator:
    """Collapses overlapping retrieval hits per build plan §12.6.

    ``text_similarity_threshold`` controls near-duplicate matching:
    * ``1.0`` (default) — exact normalized-text equality (deterministic, the
      MVP behavior and what the contract tests assert).
    * ``< 1.0`` — a ``difflib`` ratio at or above the threshold also counts as
      a duplicate (so minor token-level differences collapse too).
    """

    def __init__(self, *, text_similarity_threshold: float = 1.0) -> None:
        if not 0.0 < text_similarity_threshold <= 1.0:
            raise ValueError("text_similarity_threshold must be in (0, 1]")
        self._threshold = text_similarity_threshold

    # -- public API -------------------------------------------------------

    def deduplicate(self, candidates: list[DedupCandidate]) -> list[DedupCandidate]:
        """Return deduplicated survivors ordered by descending hit score."""
        if not candidates:
            return []

        # Three sequential passes, each collapsing one dimension. Order follows
        # the retention rules: span -> parent -> text (cross-corpus). Each pass
        # keeps the highest-scoring survivor and merges the losers' provenance.
        after_span = self._collapse(
            candidates, key=lambda c: (c.hit.document_id, c.hit.document_version, c.hit.chunk_id)
        )
        after_parent = self._collapse(
            after_span, key=lambda c: c.hit.parent_id
        )
        # Text pass may reorder winners by authority; handled inside _collapse.
        after_text = self._collapse_text(after_parent)

        after_text.sort(key=lambda c: c.hit.score, reverse=True)
        return after_text

    # -- internals --------------------------------------------------------

    def _collapse(
        self, candidates: list[DedupCandidate], *, key
    ) -> list[DedupCandidate]:
        """Group by ``key`` keeping the highest-scoring survivor."""
        groups: dict[Any, DedupCandidate] = {}
        for cand in candidates:
            k = key(cand)
            existing = groups.get(k)
            if existing is None or cand.hit.score > existing.hit.score:
                groups[k] = cand
                if existing is not None:
                    self._fold(groups[k], existing)
            else:
                self._fold(existing, cand)
        return list(groups.values())

    def _collapse_text(self, candidates: list[DedupCandidate]) -> list[DedupCandidate]:
        """Group by (near-duplicate) normalized text.

        Retention rule (§12.6 #4): keep the higher-authority source, ties broken
        by higher score. Losers are recorded as duplicate sources on the winner.
        """
        groups: dict[str, DedupCandidate] = {}
        for cand in candidates:
            text_key = normalize_text(cand.text)
            existing = self._find_text_match(groups, cand, text_key)
            if existing is None:
                groups[text_key] = cand
                continue
            # Decide the winner for this text group.
            if self._is_better(cand, existing):
                # cand wins: demote the old survivor into a duplicate source and
                # re-home it as a merged entry under cand.
                old = existing
                self._fold(cand, old)
                # Re-key the losing survivor out of the group map; the group now
                # points at cand.
                groups[text_key] = cand
                # old may itself have carried duplicate sources — already folded.
            else:
                self._fold(existing, cand)
        return list(groups.values())

    def _find_text_match(
        self, groups: dict[str, DedupCandidate], cand: DedupCandidate, text_key: str
    ) -> DedupCandidate | None:
        if self._threshold >= 1.0:
            return groups.get(text_key)
        cand_norm = text_key
        for key, existing in groups.items():
            if difflib.SequenceMatcher(None, key, cand_norm).ratio() >= self._threshold:
                return existing
        return None

    @staticmethod
    def _is_better(cand: DedupCandidate, existing: DedupCandidate) -> bool:
        """True if ``cand`` should win the text group over ``existing``."""
        if cand.authority_level != existing.authority_level:
            return cand.authority_level > existing.authority_level
        return cand.hit.score > existing.hit.score

    @staticmethod
    def _fold(keep: DedupCandidate, incoming: DedupCandidate) -> None:
        """Fold ``incoming`` into ``keep`` (keep already won the tie)."""
        for ctx in incoming.contexts:
            if ctx not in keep.contexts:
                keep.contexts.append(ctx)
        keep.duplicate_sources.append(incoming._source_record())
        keep.duplicate_sources.extend(incoming.duplicate_sources)
