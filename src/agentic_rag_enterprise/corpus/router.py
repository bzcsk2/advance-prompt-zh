"""E-016 permission-aware soft router (build plan §9.3 / Milestone 4).

The router is the *only* component that decides which corpora a query should hit.
It is deliberately deterministic and model-free: it consumes **only** the
discoverable, capability-eligible corpora returned by
``CorpusRegistry.resolve_candidates`` and ranks them with a *query-sensitive*
relevance signal combined with the registry-declared ``authority_level``. The
model is never given the full corpus map, and a non-discoverable corpus can never
enter the ranked output.

Scoring (build plan §9.3, all deterministic — no model, no query leakage):

* ``relevance`` = normalized-term overlap between the query and the corpus'
  ``name`` / ``description`` / ``domain`` / ``corpus_id`` / ``capability_ids``
  term bag, in ``[0, 1]``;
* ``authority`` = ``authority_level / 100`` in ``[0, 1]``;
* ``score`` = ``_RELEVANCE_WEIGHT * relevance + _AUTHORITY_WEIGHT * authority``,
  clamped to ``[0, 1]`` (matches ``CorpusCandidate.score`` field ``ge=0, le=1``).

Route policy (build plan §9.3):

* ``high`` confidence  → Top-1 (fallback expansion to Top-2 is signalled to the
  caller via the extra ranked candidate, but the primary is a single corpus);
* ``medium`` confidence → Top-2;
* ``low`` confidence   → Top-3 + ``fallback_search=True`` (the query matched no
  corpus term, so we broaden and probe instead of hard-routing to authority).

``fallback_search`` is ``True`` only for the ``low`` branch (§9.3: "禁止硬路由后
立即把未命中解释为无答案").
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from agentic_rag_enterprise.corpus.registry import CorpusRegistry
from agentic_rag_enterprise.domain.corpus import CorpusConfig
from agentic_rag_enterprise.domain.security import SecurityContext

RouteConfidence = Literal["high", "medium", "low"]

# Deterministic score composition weights (query relevance dominates; authority is
# a secondary, policy-reviewed tie / boost signal). Fixed, not model-derived.
_RELEVANCE_WEIGHT = 0.7
_AUTHORITY_WEIGHT = 0.3

# Confidence thresholds (deterministic, testable).
_HIGH_RELEVANCE = 0.5
_HIGH_GAP = 0.15

_TOKEN_RE = re.compile(r"[a-z0-9]+")

# Very common English words carry no routing signal; dropping them keeps a query
# like "how to handle failures in tickets" from matching every corpus via "how".
_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "do",
        "for",
        "from",
        "how",
        "i",
        "in",
        "is",
        "it",
        "of",
        "on",
        "or",
        "the",
        "to",
        "use",
        "what",
        "when",
        "where",
        "which",
        "with",
    }
)


def _tokenize(text: str) -> set[str]:
    """Lower-case, alnum-token, stopword-filtered term set (deterministic)."""
    return {t for t in _TOKEN_RE.findall(text.lower()) if t not in _STOPWORDS}


def _corpus_terms(corpus: CorpusConfig) -> set[str]:
    """The routable term bag for a corpus (name/description/domain/id/capabilities)."""
    parts = [
        corpus.name,
        corpus.description,
        corpus.domain,
        corpus.corpus_id.replace("_", " "),
        " ".join(corpus.capability_ids),
    ]
    return _tokenize(" ".join(parts))


@dataclass(frozen=True)
class CorpusCandidate:
    """A ranked, discoverable corpus the router scored for a query.

    ``rationale`` is derived purely from the *selected* candidate (relevance +
    authority) and never references any denied / undiscoverable corpus, so the
    route cannot leak the existence of corpora the caller may not see.
    """

    corpus_id: str
    name: str
    authority_level: int
    relevance: float
    score: float
    rationale: str


@dataclass(frozen=True)
class CorpusRoute:
    """The deterministic soft route for a query (build plan §9.3).

    ``candidates`` contains ONLY discoverable corpora selected by the confidence
    policy. ``route_confidence`` and ``fallback_search`` mirror §9.3 exactly. The
    full corpus map is never materialised here.
    """

    query: str
    candidates: tuple[CorpusCandidate, ...]
    route_confidence: RouteConfidence
    fallback_search: bool
    truncated_from: int


class CorpusRouter:
    """Deterministic, query-sensitive, permission-aware soft router (§9.3).

    Input is constrained to ``registry.resolve_candidates`` output — the router can
    never rank a non-discoverable corpus. Scoring combines a query-relevance signal
    with the registry-declared ``authority_level``; the ranking is stable and
    testable (score desc, authority desc, corpus_id asc).
    """

    def route(
        self,
        query: str,
        security_context: SecurityContext,
        registry: CorpusRegistry,
        *,
        limit: int | None = None,
    ) -> CorpusRoute:
        """Score discoverable corpora for ``query`` and apply the §9.3 policy.

        Args:
            query: The user question. Its normalized terms are matched against each
                corpus' term bag; the query is never sent to a model.
            security_context: The runtime-injected security context; forwarded to the
                registry so discoverability is enforced.
            registry: The ``CorpusRegistry`` (E-015). Only its discoverable candidates
                are ever considered.
            limit: Optional hard cap on returned candidates. When ``None`` the
                confidence policy decides the count (Top-1/2/3). When given it further
                truncates the policy result (never widens it).

        Returns:
            A :class:`CorpusRoute` with the policy-selected, discoverable candidates,
            plus ``route_confidence`` and ``fallback_search`` per §9.3.
        """
        if limit is not None and limit < 1:
            raise ValueError("limit must be >= 1 when provided")

        candidates = registry.resolve_candidates(query, security_context, limit=1_000_000)
        query_terms = _tokenize(query)
        scored = self._score(candidates, query_terms)

        confidence, policy_count, fallback = self._classify(scored)
        take = policy_count if limit is None else min(policy_count, limit)
        selected = scored[:take]

        return CorpusRoute(
            query=query,
            candidates=tuple(selected),
            route_confidence=confidence,
            fallback_search=fallback,
            truncated_from=len(candidates),
        )

    @staticmethod
    def _score(corpora: list[CorpusConfig], query_terms: set[str]) -> list[CorpusCandidate]:
        """Score + rank candidates deterministically (score desc, authority desc, id asc)."""
        out: list[CorpusCandidate] = []
        n_query = len(query_terms)
        for c in corpora:
            terms = _corpus_terms(c)
            overlap = len(query_terms & terms)
            relevance = (overlap / n_query) if n_query else 0.0
            authority = c.authority_level / 100.0
            score = _RELEVANCE_WEIGHT * relevance + _AUTHORITY_WEIGHT * authority
            score = max(0.0, min(1.0, score))
            out.append(
                CorpusCandidate(
                    corpus_id=c.corpus_id,
                    name=c.name,
                    authority_level=c.authority_level,
                    relevance=round(relevance, 6),
                    score=round(score, 6),
                    rationale=f"relevance={relevance:.2f} authority={c.authority_level}",
                )
            )
        out.sort(key=lambda x: (-x.score, -x.authority_level, x.corpus_id))
        return out

    @staticmethod
    def _classify(
        scored: list[CorpusCandidate],
    ) -> tuple[RouteConfidence, int, bool]:
        """Map the ranked scores to (confidence, candidate count, fallback) per §9.3."""
        if not scored:
            return ("low", 0, True)

        top = scored[0]
        gap = top.score - (scored[1].score if len(scored) > 1 else 0.0)

        # low: the query matched no corpus term at all → broaden + probe (§9.3),
        # never hard-route to raw authority.
        if top.relevance <= 0.0:
            return ("low", min(3, len(scored)), True)

        # high: a clearly dominant, strongly-relevant top candidate → Top-1.
        if top.relevance >= _HIGH_RELEVANCE and gap >= _HIGH_GAP:
            return ("high", 1, False)

        # medium: some relevance, no dominant winner → Top-2.
        return ("medium", min(2, len(scored)), False)
