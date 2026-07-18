"""E-016 permission-aware soft router (build plan §9.3 / Milestone 4).

The router is the *only* component that decides which corpora a query should hit.
It is deliberately deterministic and model-free: it consumes **only** the
discoverable, capability-eligible corpora returned by
``CorpusRegistry.resolve_candidates`` and ranks them by a registry-declared signal
(``authority_level``), tie-broken by ``corpus_id``. The model is never given the
full corpus map, and a non-discoverable corpus can never enter the ranked output.

The router produces a ranked ``CorpusRoute`` of at most ``limit`` candidates. The
caller (the multi-corpus retrieval / chat entry) decides how many to actually query;
the router does not perform retrieval and never sees Evidence.
"""

from __future__ import annotations

from dataclasses import dataclass

from agentic_rag_enterprise.corpus.registry import CorpusRegistry
from agentic_rag_enterprise.domain.corpus import CorpusConfig
from agentic_rag_enterprise.domain.security import SecurityContext


@dataclass(frozen=True)
class CorpusCandidate:
    """A ranked, discoverable corpus the router selected for a query.

    ``rationale`` is derived purely from the *selected* candidate (e.g.
    ``"authority=80"``) and must never reference any denied / undiscoverable corpus,
    so the route cannot leak the existence of corpora the caller may not see.
    """

    corpus_id: str
    name: str
    authority_level: int
    score: float
    rationale: str


@dataclass(frozen=True)
class CorpusRoute:
    """The deterministic, top-N ranked route for a query.

    ``candidates`` contains ONLY discoverable corpora (``truncated_from`` says how
    many discoverable candidates existed before truncation). The full corpus map is
    never materialised here.
    """

    query: str
    candidates: tuple[CorpusCandidate, ...]
    truncated_from: int


class CorpusRouter:
    """Deterministic, permission-aware soft router (build plan §9.3).

    Scoring is ``authority_level`` (a registry-declared, policy-reviewed value, not
    a model output), tie-broken by ``corpus_id`` ascending, so the ranking is stable
    and testable. Input is constrained to ``registry.resolve_candidates`` output —
    the router can never rank a non-discoverable corpus.
    """

    def route(
        self,
        query: str,
        security_context: SecurityContext,
        registry: CorpusRegistry,
        *,
        limit: int = 2,
    ) -> CorpusRoute:
        """Rank discoverable corpora for ``query`` and return the top ``limit``.

        Args:
            query: The user question (used only for candidate resolution / logging;
                not sent to any model).
            security_context: The runtime-injected security context; forwarded to the
                registry so discoverability is enforced.
            registry: The ``CorpusRegistry`` (E-015). Only its discoverable candidates
                are ever considered.
            limit: Maximum number of candidates to return (default 2). Must be >= 1.

        Returns:
            A :class:`CorpusRoute` with at most ``limit`` ranked, discoverable
            candidates.
        """
        if limit < 1:
            raise ValueError("limit must be >= 1")

        candidates = registry.resolve_candidates(query, security_context, limit=1_000_000)
        ranked = self._rank(candidates)
        truncated = ranked[:limit]
        return CorpusRoute(
            query=query,
            candidates=tuple(
                CorpusCandidate(
                    corpus_id=c.corpus_id,
                    name=c.name,
                    authority_level=c.authority_level,
                    score=float(c.authority_level),
                    rationale=f"authority={c.authority_level}",
                )
                for c in truncated
            ),
            truncated_from=len(candidates),
        )

    @staticmethod
    def _rank(corpora: list[CorpusConfig]) -> list[CorpusConfig]:
        """Stable ranking by descending authority_level, ascending corpus_id."""
        return sorted(
            corpora,
            key=lambda c: (-c.authority_level, c.corpus_id),
        )
