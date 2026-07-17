"""E-014 API schemas (build plan §6: ``api/`` adapter only, no business rules)."""

from __future__ import annotations

from agentic_rag_enterprise.answer.envelope import AnswerEnvelope
from pydantic import BaseModel


class ChatRequest(BaseModel):
    """Inbound chat request.

    The security-context fields are **runtime-injected** from trusted request
    metadata (gateway / IAM). They are NEVER supplied by, or read back from, the
    model (build plan §5.4). In the Internal MVP they travel in the body as a
    stand-in for real auth injection.
    """

    query: str
    corpus_id: str = "eng"

    # --- runtime-injected security context (not model-supplied) ---
    tenant_id: str
    user_id: str
    policy_version: str
    request_id: str = ""
    session_id: str = ""
    roles: list[str] = []
    groups: list[str] = []
    allowed_security_levels: list[str] | None = None
    allowed_corpus_ids: list[str] | None = None
    is_admin: bool = False
    permissions: list[str] = []


# The response is exactly the validated E-013 AnswerEnvelope (FastAPI serializes
# the frozen pydantic model directly). Aliased for a stable API surface name.
ChatResponse = AnswerEnvelope
