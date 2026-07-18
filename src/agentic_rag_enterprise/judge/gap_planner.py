"""E-020 Gap Planner (build plan §14.4).

Turns the Coverage Judge result into the next-round retrieval plan. Per §14.4 it
MUST only emit queries for facts that are still ``missing`` / ``partially_supported``
(and ``not_retrievable`` — nothing was found for them); it must never re-query for
already-supported facts, and it must not repeat a query that has already been
executed. The single answered corpus is the only target (Milestone 3 is
single-corpus; multi-corpus routing is M4).
"""

from __future__ import annotations

from agentic_rag_enterprise.judge.models import (
    FactStatus,
    GapRetrievalPlan,
    SufficiencyResult,
)


class GapPlanner:
    """Derive gap-retrieval queries from uncovered Required Facts."""

    name = "deterministic"

    def plan(
        self,
        coverage: SufficiencyResult,
        *,
        prior_queries: list[str] | None = None,
        corpus_id: str | None = None,
    ) -> GapRetrievalPlan:
        prior = set(prior_queries or [])
        queries: list[str] = []
        fact_ids: list[str] = []

        for fc in coverage.fact_coverage:
            if fc.status not in (
                FactStatus.MISSING,
                FactStatus.PARTIALLY_SUPPORTED,
                FactStatus.NOT_RETRIEVABLE,
            ):
                continue
            fact_ids.append(fc.fact_id)
            candidate = fc.next_queries[0] if fc.next_queries else fc.fact_id
            if candidate and candidate not in prior:
                queries.append(candidate)
                prior.add(candidate)

        return GapRetrievalPlan(
            queries=tuple(queries),
            target_corpus_ids=(corpus_id,) if corpus_id else (),
            fact_ids=tuple(fact_ids),
            reason=(
                "gap queries for missing/partially_supported facts"
                if queries
                else "no remaining gap queries"
            ),
        )
