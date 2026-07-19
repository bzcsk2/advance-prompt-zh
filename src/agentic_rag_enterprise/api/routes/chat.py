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
from agentic_rag_enterprise.storage.checkpoint_store import (
    CheckpointIdentityConflict,
    ResumeAuthError,
)

logger = logging.getLogger("agentic_rag_enterprise.api.chat")

# Fixed, generic messages returned to the client. They never embed internal
# identifiers or the underlying exception text (build plan §12.8).
_MSG_BACKEND_UNAVAILABLE = "The retrieval backend is temporarily unavailable."
_MSG_MODEL_UNAVAILABLE = "The answer service is temporarily unavailable."
_MSG_INTERNAL_ERROR = "An internal error occurred."
_MSG_RESUME_BAD_REQUEST = "Resume requested without a run_id."
_MSG_RESUME_CONFLICT = "The requested run_id is already bound to another session."

_STATUS_CHECKPOINT_NOT_FOUND = status.HTTP_404_NOT_FOUND
_STATUS_RESUME_CONFLICT = status.HTTP_409_CONFLICT


def chat_v1(
    request: ChatRequest,
    ctx: SecurityContext = Depends(get_security_context),
    service: ChatService = Depends(get_chat_service),
) -> ChatResponse:
    try:
        if request.resume:
            # E-023 resume branch: re-authorize the earlier checkpoint against
            # CURRENT state and continue. ``resume`` without ``run_id`` is a client
            # error (fail closed — never synthesize from a non-existent checkpoint).
            if request.run_id is None:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=_MSG_RESUME_BAD_REQUEST,
                )
            return service.resume_run(request.run_id, ctx)
        if request.run_id is not None:
            # E-023 P1-1: a ``run_id`` WITHOUT ``resume`` starts a checkpointed
            # iteration run through the default judge, so a later ``resume`` with
            # the same ``run_id`` can recover it. (With no judge configured the
            # service degrades to single-pass and ignores ``run_id``.)
            return service.answer_with_iteration(
                request.query,
                ctx,
                request.corpus_id,
                run_id=request.run_id,
                judge=service.judge,
            )
        return service.answer(request.query, ctx, request.corpus_id, run_id=request.run_id)
    except CheckpointIdentityConflict as exc:
        # A ``run_id`` reused under a different principal/query is refused so it
        # cannot hijack another session's checkpoint (build plan §5.4). The
        # client learns only that the id is taken — never the binding details.
        logger.warning("Checkpoint identity conflict for run_id %s", request.run_id)
        raise HTTPException(
            status_code=_STATUS_RESUME_CONFLICT,
            detail=_MSG_RESUME_CONFLICT,
        ) from exc
    except ResumeAuthError as exc:
        # A resume that cannot be re-authorized (foreign checkpoint, stale policy,
        # undiscoverable corpus, or revoked evidence) is a NOT-FOUND at the API
        # boundary — the client must not learn *why* (no internal detail leak).
        logger.exception("Resume authorization failed for request %s", ctx.request_id)
        raise HTTPException(
            status_code=_STATUS_CHECKPOINT_NOT_FOUND,
            detail=_MSG_INTERNAL_ERROR,
        ) from exc
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
    except HTTPException:
        # A handler-raised HTTPException (e.g. resume-without-run_id 400) is a
        # deliberate client-facing status; re-raise it, never relabel as 500.
        raise
    except Exception as exc:  # noqa: BLE001 - surface as 500; never mask as answer
        logger.exception("Unexpected error for request %s", ctx.request_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=_MSG_INTERNAL_ERROR,
        ) from exc
