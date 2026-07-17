"""E-014 LLM structured-output schema for claim extraction.

The chat service asks the model to return this schema. It reuses E-013's frozen
``Claim`` so the verified claims flow straight into ``build_answer_envelope``.
The ``draft_answer`` is advisory only — per E-013's fail-closed rule the final
answer is always rendered from the *verified* claims, never from this prose.
"""

from pydantic import BaseModel

from agentic_rag_enterprise.answer import Claim


class ClaimExtraction(BaseModel):
    """Structured output the synthesis model must return."""

    draft_answer: str
    claims: list[Claim]
