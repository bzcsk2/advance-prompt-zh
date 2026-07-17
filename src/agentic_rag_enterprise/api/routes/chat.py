"""E-014 chat route (build plan §6: FastAPI adapter, no business rules).

The endpoint is a thin adapter: it injects the runtime ``SecurityContext``, calls
the ``ChatService``, and returns the validated ``AnswerEnvelope``. It never
exposes ``denied_reasons`` / internal telemetry, and it never masks a backend or
model fault as a grounded answer or a refusal.

Registered on the FastAPI ``app`` in ``api/main.py`` (this environment's
``include_router`` is a no-op, so the repo's ``@app.post`` style is used).
"""

from __future__ import annotations

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


def chat_v1(
    request: ChatRequest,
    ctx: SecurityContext = Depends(get_security_context),
    service: ChatService = Depends(get_chat_service),
) -> ChatResponse:
    try:
        return service.answer(request.query, ctx, request.corpus_id)
    except FastPathBackendError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"retrieval backend unavailable: {exc}",
        ) from exc
    except ModelInvocationError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
        ) from exc
    except ChatServiceError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        ) from exc
    except Exception as exc:  # noqa: BLE001 - surface as 500; never mask as answer
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"chat service error: {exc}",
        ) from exc
