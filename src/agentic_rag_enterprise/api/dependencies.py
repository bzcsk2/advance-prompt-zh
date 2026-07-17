"""E-014 API dependencies (build plan §6: adapter only).

Builds the runtime-injected :class:`SecurityContext` from **trusted request
headers** (set by the gateway / IAM) and provides the shared :class:`ChatService`.

Security model (build plan §5.4):

* The client **body** carries only ``query`` + ``corpus_id``. It never carries
  identity / authorization / policy data.
* The :class:`SecurityContext` is built exclusively from request **headers** that
  a trusted gateway injects (``x-tenant-id``, ``x-user-id``, …). A client that
  smuggles ``tenant_id`` / ``is_admin`` / ``permissions`` into the JSON body is
  ignored — those fields no longer exist on the request model.
* ``is_admin`` and ``permissions`` are **server-decided** (a trusted header or
  deployment config). They are never taken from the model output (the LLM is not
  a security boundary) and are not inferable from anything the client sends.

Process-wide state: ``get_chat_service`` returns the single shared default
service (see :mod:`agentic_rag_enterprise.services.container`), so documents
ingested through that container are immediately retrievable by ``POST /v1/chat``
without any external dependency. The service is overridable in tests via
FastAPI's dependency-override mechanism.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import Header

from agentic_rag_enterprise.api.schemas import ChatRequest
from agentic_rag_enterprise.domain.security import SecurityContext
from agentic_rag_enterprise.services.chat_service import ChatService
from agentic_rag_enterprise.services.container import get_default_container


def _new_id() -> str:
    return uuid.uuid4().hex


def _split_csv(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


def get_security_context(
    request: ChatRequest,
    x_tenant_id: Annotated[str, Header(alias="x-tenant-id")],
    x_user_id: Annotated[str, Header(alias="x-user-id")],
    x_policy_version: Annotated[str, Header(alias="x-policy-version")] = "default",
    x_request_id: Annotated[str, Header(alias="x-request-id")] = "",
    x_session_id: Annotated[str, Header(alias="x-session-id")] = "",
    x_roles: Annotated[str, Header(alias="x-roles")] = "",
    x_groups: Annotated[str, Header(alias="x-groups")] = "",
    x_security_levels: Annotated[str, Header(alias="x-security-levels")] = "public,internal",
    x_corpus_ids: Annotated[str, Header(alias="x-corpus-ids")] = "",
    x_is_admin: Annotated[str, Header(alias="x-is-admin")] = "false",
    x_permissions: Annotated[str, Header(alias="x-permissions")] = "",
) -> SecurityContext:
    """Construct the SecurityContext from trusted request headers.

    These fields are runtime-injected by the gateway / IAM; they are never taken
    from or influenced by the request body or the model output (build plan §5.4).
    The model never sees them.
    """
    # is_admin / permissions are server-decided. The only client-controllable
    # signal is a *trusted* gateway header; the body has no such field. We parse
    # it defensively so a malformed value falls back to the safe default.
    is_admin = str(x_is_admin).strip().lower() in {"1", "true", "yes", "on"}
    permissions = _split_csv(x_permissions)
    allowed_corpus_ids = _split_csv(x_corpus_ids) or None

    return SecurityContext(
        request_id=x_request_id or _new_id(),
        session_id=x_session_id or _new_id(),
        tenant_id=x_tenant_id,
        user_id=x_user_id,
        roles=_split_csv(x_roles),
        groups=_split_csv(x_groups),
        allowed_security_levels=_split_csv(x_security_levels),
        allowed_corpus_ids=allowed_corpus_ids,
        policy_version=x_policy_version,
        is_admin=is_admin,
        permissions=permissions,
    )


def get_chat_service() -> ChatService:
    """Return the shared, runnable default ChatService.

    The default path is fully wired (in-memory Qdrant, deterministic encoders, a
    hermetic synthesis model that registers ``ClaimExtraction``, and a storage
    stack shared with the ingestion pipeline) so ``POST /v1/chat`` works out of
    the box with no external dependency. Callers (tests, deployments) may
    override this dependency to inject a different service.
    """
    return get_default_container().service
