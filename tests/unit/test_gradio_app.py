"""Unit tests for the E-014 minimal Gradio adapter.

The adapter must be import-safe without gradio installed (lazy import), raise a
clear error when gradio is absent, and (when gradio is present) build an
interface that calls the shared ChatService. gradio is an optional dependency,
so the build-with-gradio case is skipped when it is not installed.
"""

import pytest

from agentic_rag_enterprise.domain.security import SecurityContext
from agentic_rag_enterprise.ui.gradio_app import build_gradio_app


def _ctx() -> SecurityContext:
    return SecurityContext(
        request_id="r",
        session_id="s",
        tenant_id="t1",
        user_id="u1",
        policy_version="1.0",
    )


class _DummyService:
    """Stand-in ChatService: returns an envelope-shaped object."""

    def answer(self, query: str, ctx: SecurityContext, corpus_id: str):
        class _Env:
            answer_markdown = "hi"

        return _Env()


def test_module_imports_without_gradio() -> None:
    # Importing the module must not require gradio (lazy import).
    import agentic_rag_enterprise.ui.gradio_app as mod  # noqa: F401

    assert hasattr(mod, "build_gradio_app")


def test_build_raises_without_gradio() -> None:
    with pytest.raises(RuntimeError, match="gradio"):
        build_gradio_app(_DummyService())


def test_builds_with_gradio_when_available() -> None:
    gradio = pytest.importorskip("gradio")
    demo = build_gradio_app(
        _DummyService(),
        security_context_factory=_ctx,
    )
    assert isinstance(demo, gradio.Blocks)
