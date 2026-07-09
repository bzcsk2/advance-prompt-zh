from agentic_rag_enterprise.schemas import QueryPlan, SubQuestion


class PlannerAgent:
    """Decompose a user query into required facts and subquestions.

    The production implementation should call an LLM with structured output.
    This scaffold keeps deterministic behavior for tests and interface design.
    """

    def plan(self, query: str) -> QueryPlan:
        return QueryPlan(
            task_type="single_hop",
            required_facts=[query],
            subquestions=[
                SubQuestion(
                    id="q1",
                    question=query,
                    target_corpora=[],
                )
            ],
        )
