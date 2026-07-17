"""E-014 chat route (build plan §6: FastAPI adapter, no business rules).

The endpoint is a thin adapter: it injects the runtime ``SecurityContext``, calls
the ``ChatService``, and returns the validated ``AnswerEnvelope``. It never
exposes ``denied_reasons`` / internal telemetry, and it never masks a backend or
model fault as a grounded answer or a refusal.

Error handling is fail-closed for the *caller*: every 5xx returns a fixed,
generic message. Internal identifiers (tenant ids, evidence ids, corpus ids) and
the underlying exception text must NEVER leave the process — they are written to
the internal log only (build plan §5.4 / §12.8).
"""

from __future__ import annotations

import logging

from fastapi import Depends, HTTPException, status

from agentic_rag_enterprise.api.dependencies import get_chat_service, get_security_context
from agentic_rag_enterprise.api.schemas import ChatRequest, ChatResponse
from agentic_rag_enterprise.domain.security import SecurityContext
from agentic_rag_enterprise.retrieval.fast_path import FastPathBackendError
from agentic_rag_enterprise.services.chat_service import (
    ChatService,
    ChatServiceError,
    ModelInvocationError,
)

logger = logging.getLogger("agentic_rag_enterprise.api.chat")

# Fixed, generic messages returned to the client. They never embed internal
# identifiers or the underlying exception text (build plan §12.8).
_MSG_BACKEND_UNAVAILABLE = "The retrieval backend is temporarily unavailable."
_MSG_MODEL_UNAVAILABLE = "The answer service is temporarily unavailable."
_MSG_INTERNAL_ERROR = "An internal error occurred."


def chat_v1(
    request: ChatRequest,
    ctx: SecurityContext = Depends(get_security_context),
    service: ChatService = Depends(get_chat_service),
) -> ChatResponse:
    try:
        return service.answer(request.query, ctx, request.corpus_id)
    except FastPathBackendError as exc:
        logger.exception("FastPath backend error for request %s", ctx.request_id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_BACKEND_UNAVAILABLE,
        ) from exc
    except ModelInvocationError as exc:
        logger.exception("Model invocation error for request %s", ctx.request_id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=_MSG_MODEL_UNAVAILABLE,
        ) from exc
    except ChatServiceError as exc:
        logger.exception("Chat service error for request %s", ctx.request_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=_MSG_INTERNAL_ERROR,
        ) from exc
    except Exception as exc:  # noqa: BLE001 - surface as 500; never mask as answer
        logger.exception("Unexpected error for request %s", ctx.request_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=_MSG_INTERNAL_ERROR,
        ) from exc
