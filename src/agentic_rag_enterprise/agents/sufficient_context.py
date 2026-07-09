from agentic_rag_enterprise.schemas import Evidence, QueryPlan, SufficiencyDecision, SufficiencyStatus


class SufficientContextAgent:
    """Judge whether retrieved evidence is sufficient to answer the query."""

    def judge(
        self,
        query: str,
        plan: QueryPlan,
        evidence: list[Evidence],
    ) -> SufficiencyDecision:
        if not evidence:
            return SufficiencyDecision(
                status=SufficiencyStatus.INSUFFICIENT,
                missing_facts=plan.required_facts or [query],
                next_queries=[query],
                reason="No evidence has been retrieved yet.",
            )

        return SufficiencyDecision(
            status=SufficiencyStatus.SUFFICIENT,
            covered_facts=plan.required_facts,
            reason="Evidence is present. Replace this heuristic with an LLM-based sufficiency judge.",
        )
