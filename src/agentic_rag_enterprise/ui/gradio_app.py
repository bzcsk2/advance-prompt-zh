"""E-014 minimal Gradio chat adapter (build plan §2.2 / §6: ``ui/``).

A thin adapter that points the shared :class:`ChatService` at a Gradio chat UI.
``gradio`` is an OPTIONAL dependency and is imported lazily inside
:func:`build_gradio_app`, so this module is import-safe without gradio installed
and the unit gates do not require gradio.

The runtime ``SecurityContext`` is injected via ``security_context_factory`` —
the UI never supplies tenant / identity / policy data, and the model never sees
it (build plan §5.4).
"""

from __future__ import annotations

from typing import Callable

from agentic_rag_enterprise.domain.security import SecurityContext
from agentic_rag_enterprise.services.chat_service import ChatService


def build_gradio_app(
    service: ChatService,
    *,
    corpus_id: str = "eng",
    security_context_factory: Callable[[], SecurityContext] | None = None,
):
    """Build a minimal Gradio chat interface backed by ``service``.

    Args:
        service: The shared chat service (same one behind ``POST /v1/chat``).
        corpus_id: The single corpus the Internal MVP serves.
        security_context_factory: Callable that returns the runtime-injected
            ``SecurityContext`` for each turn. Required — the UI must not derive
            identity from model output.

    Returns:
        A ``gradio.Blocks`` demo.

    Raises:
        RuntimeError: if gradio is not installed, or if no
            ``security_context_factory`` is supplied.
    """
    try:
        import gradio as gr  # type: ignore[import-not-found]
    except ImportError as exc:  # optional dependency
        raise RuntimeError(
            "gradio is not installed; install the 'gradio' extra to run the UI adapter"
        ) from exc

    if security_context_factory is None:
        raise RuntimeError(
            "a security_context_factory is required to inject the runtime "
            "SecurityContext (the UI must not supply identity/policy data)"
        )

    def _respond(message: str, _history):
        ctx = security_context_factory()
        envelope = service.answer(message, ctx, corpus_id)
        return envelope.answer_markdown

    demo = gr.ChatInterface(_respond, title="Agentic RAG Enterprise")
    return demo
