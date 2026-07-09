from agentic_rag_enterprise.schemas import Evidence, GroundedAnswer, SufficiencyDecision, SufficiencyStatus


class SynthesisAgent:
    """Produce grounded answers from evidence and sufficiency decisions."""

    def synthesize(
        self,
        query: str,
        evidence: list[Evidence],
        decision: SufficiencyDecision,
    ) -> GroundedAnswer:
        if decision.status != SufficiencyStatus.SUFFICIENT:
            return GroundedAnswer(
                answer="I do not have sufficient evidence to answer this question reliably.",
                citations=[],
                confidence="low",
                completeness_note=decision.reason,
                abstained=True,
            )

        citation_ids = [item.evidence_id for item in evidence]
        evidence_preview = "\n".join(f"- {item.text}" for item in evidence[:3])
        return GroundedAnswer(
            answer=(
                "Grounded answer generation is not implemented yet. "
                "Use the following evidence as the synthesis input:\n" + evidence_preview
            ),
            citations=citation_ids,
            confidence="medium",
            completeness_note="Sufficient context was accepted by the current judge.",
            abstained=False,
        )
