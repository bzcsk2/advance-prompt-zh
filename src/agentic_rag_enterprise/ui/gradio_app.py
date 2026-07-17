"""E-014 minimal Gradio chat adapter (build plan §2.2 / §6: ``ui/``).

A thin adapter that points the shared :class:`ChatService` at a Gradio chat UI.
``gradio`` is an OPTIONAL dependency and is imported lazily inside
:func:`build_gradio_app`, so this module is import-safe without gradio installed
and the unit gates do not require gradio.

The runtime ``SecurityContext`` is injected via ``security_context_factory`` —
the UI never supplies tenant / identity / policy data, and the model never sees
it (build plan §5.4).

The rendered answer carries more than the raw ``answer_markdown``: it shows the
grounded answer, the **citations** (resolved Evidence sources), **Evidence
snippets**, and the **single corpus** the Internal MVP serves (build plan
§2.2 single-corpus user loop).
"""

from __future__ import annotations

from typing import Any, Callable

from agentic_rag_enterprise.domain.security import SecurityContext
from agentic_rag_enterprise.services.chat_service import ChatService


def _snippet(text: str, limit: int = 280) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + " …"


def render_envelope(envelope: Any, *, corpus_id: str) -> str:
    """Render a chat-ready markdown view of an ``AnswerEnvelope``.

    Includes the grounded answer, the resolved citations (Evidence sources),
    Evidence snippets, and a single-corpus entry — never internal identifiers
    the model must not see (build plan §12.8).
    """
    parts: list[str] = []

    parts.append(envelope.answer_markdown or "_(no answer)_")

    citations = list(getattr(envelope, "citations", ()) or ())
    evidence = list(getattr(envelope, "evidence", ()) or ())
    claims = list(getattr(envelope, "claims", ()) or ())

    if citations:
        parts.append("\n---\n\n**Sources**")
        for cit in citations:
            coord = f"{cit.corpus_id}/{cit.document_id}@{cit.document_version}"
            if cit.section_path:
                coord += " › " + " › ".join(cit.section_path)
            if cit.page_number is not None:
                coord += f" (p.{cit.page_number})"
            parts.append(f"{cit.index}. `{coord}` — evidence `{cit.evidence_id}`")

    if evidence:
        parts.append("\n**Evidence**")
        for ev in evidence:
            coord = f"{ev.corpus_id}/{ev.document_id}@{ev.document_version}"
            parts.append(f"- `[{ev.evidence_id}]` {coord}: {_snippet(ev.text)}")

    if claims:
        parts.append("\n**Claims**")
        for c in claims:
            parts.append(f"- {c.text}")

    # Single-corpus document entry (Internal MVP serves exactly one corpus).
    corpora = list(getattr(envelope, "corpora_used", ()) or ())
    served = corpora[0] if corpora else corpus_id
    parts.append(f"\n**Corpus:** {served}")

    return "\n".join(parts)


def gradio_respond(
    message: str,
    history: Any,
    *,
    service: ChatService,
    security_context_factory: Callable[[], SecurityContext],
    corpus_id: str = "eng",
) -> str:
    """Stateless respond used by the Gradio ``ChatInterface``.

    Kept as a module-level function (not a closure) so it is directly unit-
    testable without constructing a ``gradio.Blocks``.
    """
    ctx = security_context_factory()
    envelope = service.answer(message, ctx, corpus_id)
    return render_envelope(envelope, corpus_id=corpus_id)


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
        return gradio_respond(
            message,
            _history,
            service=service,
            security_context_factory=security_context_factory,
            corpus_id=corpus_id,
        )

    demo = gr.ChatInterface(_respond, title="Agentic RAG Enterprise")
    return demo
