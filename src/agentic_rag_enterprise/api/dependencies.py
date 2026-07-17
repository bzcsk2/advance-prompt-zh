"""E-014 API dependencies (build plan §6: adapter only).

Builds the runtime-injected :class:`SecurityContext` from trusted request
metadata and provides the :class:`ChatService`. The service is overridable in
tests via FastAPI's dependency-override mechanism.
"""

from __future__ import annotations

import uuid


from agentic_rag_enterprise.api.schemas import ChatRequest
from agentic_rag_enterprise.domain.security import SecurityContext
from agentic_rag_enterprise.services.chat_service import ChatService
from agentic_rag_enterprise.services.composition import build_chat_service_from_settings


def _new_id() -> str:
    return uuid.uuid4().hex


def get_security_context(request: ChatRequest) -> SecurityContext:
    """Construct the SecurityContext from trusted request metadata.

    These fields are runtime-injected (gateway / IAM); they are never taken from
    or influenced by model output (build plan §5.4). The model never sees them.
    """
    return SecurityContext(
        request_id=request.request_id or _new_id(),
        session_id=request.session_id or _new_id(),
        tenant_id=request.tenant_id,
        user_id=request.user_id,
        roles=list(request.roles),
        groups=list(request.groups),
        allowed_security_levels=request.allowed_security_levels or ["public", "internal"],
        allowed_corpus_ids=request.allowed_corpus_ids,
        policy_version=request.policy_version,
        is_admin=request.is_admin,
        permissions=list(request.permissions),
    )


def get_chat_service() -> ChatService:
    """Default ChatService assembly from runtime settings.

    Real embedding encoders / non-fake model providers are deferred; callers
    (deployments, tests) may override this dependency to inject fakes.
    """
    return build_chat_service_from_settings()
