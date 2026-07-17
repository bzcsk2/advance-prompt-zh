"""E-014 API schemas (build plan §6: ``api/`` adapter only, no business rules).

The request body carries ONLY the user's question and the target corpus. All
identity / authorization / policy fields are **runtime-injected from trusted
request metadata** (HTTP headers set by the gateway / IAM), never supplied by
the client body (build plan §5.4). A client must never be able to assert
``tenant_id``, ``is_admin``, ``permissions`` etc. — doing so would void the
API-layer authorization.

The response is the validated E-013 ``AnswerEnvelope``.
"""

from __future__ import annotations

from agentic_rag_enterprise.answer.envelope import AnswerEnvelope
from pydantic import BaseModel


class ChatRequest(BaseModel):
    """Inbound chat request.

    The body MUST NOT carry any security-context field. The
    :class:`~agentic_rag_enterprise.domain.security.SecurityContext` is built
    entirely from trusted request headers (see
    :func:`agentic_rag_enterprise.api.dependencies.get_security_context`). The
    model never sees, and the client never controls, tenant / identity / policy
    data (build plan §5.4).
    """

    query: str
    corpus_id: str = "eng"


# The response is exactly the validated E-013 AnswerEnvelope (FastAPI serializes
# the frozen pydantic model directly). Aliased for a stable API surface name.
ChatResponse = AnswerEnvelope
