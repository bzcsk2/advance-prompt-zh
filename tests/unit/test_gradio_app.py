"""Unit tests for the E-014 Gradio adapter.

The adapter must be import-safe without gradio installed (lazy import), raise a
clear error when gradio is absent, and (when gradio is present) build an
interface that calls the shared ChatService. gradio is an optional dependency,
so the build-with-gradio case is skipped when it is not installed. The
respond/render logic is tested directly (no gradio needed) to prove the
Internal-MVP Gradio gate: it renders the answer, citations, Evidence snippets,
and the single-corpus entry — not just ``answer_markdown``.
"""

import importlib.util

import pytest

from agentic_rag_enterprise.domain.security import SecurityContext
from agentic_rag_enterprise.ui.gradio_app import build_gradio_app, gradio_respond, render_envelope


def _ctx() -> SecurityContext:
    return SecurityContext(
        request_id="r",
        session_id="s",
        tenant_id="t1",
        user_id="u1",
        policy_version="1.0",
    )


class _Citation:
    def __init__(self) -> None:
        self.index = 1
        self.evidence_id = "ev-1"
        self.corpus_id = "eng"
        self.document_id = "doc1"
        self.document_version = "v1"
        self.section_path = ("Overview",)
        self.page_number = None
        self.source_uri = "inline://doc1"


class _Evidence:
    def __init__(self) -> None:
        self.evidence_id = "ev-1"
        self.corpus_id = "eng"
        self.document_id = "doc1"
        self.document_version = "v1"
        self.text = "The planner selects corpora based on the question."


class _Claim:
    def __init__(self) -> None:
        self.text = "The planner selects corpora."


class _Envelope:
    answer_markdown = "The planner selects corpora."
    citations = (_Citation(),)
    evidence = (_Evidence(),)
    claims = (_Claim(),)
    corpora_used = ("eng",)


class _DummyService:
    """Stand-in ChatService returning a realistic envelope."""

    def answer(self, query: str, ctx: SecurityContext, corpus_id: str):
        return _Envelope()


def test_module_imports_without_gradio() -> None:
    # Importing the module must not require gradio (lazy import).
    import agentic_rag_enterprise.ui.gradio_app as mod  # noqa: F401

    assert hasattr(mod, "build_gradio_app")
    assert hasattr(mod, "gradio_respond")


def test_build_raises_without_gradio() -> None:
    # This path only exists when gradio is NOT installed. With the optional
    # "gradio" extra installed, the missing-dependency error cannot occur, so
    # skip rather than fail.
    if importlib.util.find_spec("gradio") is not None:
        pytest.skip("gradio is installed; missing-dependency path is N/A")
    with pytest.raises(RuntimeError, match="gradio"):
        build_gradio_app(_DummyService())


def test_render_shows_citations_evidence_and_corpus() -> None:
    out = gradio_respond(
        "how does planning work?",
        [],
        service=_DummyService(),
        security_context_factory=_ctx,
        corpus_id="eng",
    )
    assert "The planner selects corpora." in out
    assert "**Sources**" in out
    assert "eng/doc1@v1" in out
    assert "evidence `ev-1`" in out
    assert "**Evidence**" in out
    assert "The planner selects corpora based on the question." in out
    assert "**Corpus:** eng" in out


def test_render_entrypoint_is_single_corpus() -> None:
    # The single-corpus entry must always be present even on a minimal envelope.
    class _Min:
        answer_markdown = "ok"
        citations = ()
        evidence = ()
        claims = ()
        corpora_used = ("eng",)

    out = render_envelope(_Min(), corpus_id="eng")
    assert "**Corpus:** eng" in out


def test_builds_with_gradio_when_available() -> None:
    gradio = pytest.importorskip("gradio")
    demo = build_gradio_app(
        _DummyService(),
        security_context_factory=_ctx,
    )
    assert isinstance(demo, gradio.Blocks)
