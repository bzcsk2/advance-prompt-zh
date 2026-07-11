"""Architecture test: only SecureRetriever is the public retrieval entry point.

The hybrid search adapter must stay internal (private, not exported from the
``retrieval`` package) so callers cannot bypass the corpus-discoverability
gate and parent second-authorization by calling it directly (P2-1).
"""

import agentic_rag_enterprise.retrieval as retrieval_pkg
import pytest

from agentic_rag_enterprise.retrieval.hybrid import _HybridSearchAdapter


def test_only_secure_retriever_is_exported() -> None:
    assert "SecureRetriever" in retrieval_pkg.__all__
    assert "Retriever" in retrieval_pkg.__all__
    # The hybrid adapter must NOT be a public package export.
    assert "_HybridSearchAdapter" not in retrieval_pkg.__all__
    assert not hasattr(retrieval_pkg, "HybridRetriever")


def test_public_entry_points_are_importable() -> None:
    # The package-level export must be a real, importable binding (not just a
    # string in __all__).
    from agentic_rag_enterprise.retrieval import Retriever, SecureRetriever

    assert Retriever is not None
    assert SecureRetriever is not None
    assert "_HybridSearchAdapter" not in dir(retrieval_pkg)


def test_hybrid_adapter_not_importable_from_package() -> None:
    with pytest.raises(ImportError):
        from agentic_rag_enterprise.retrieval import HybridRetriever  # noqa: F401


def test_hybrid_adapter_remains_available_internally() -> None:
    # Internal modules (and tests) may still use the private adapter directly.
    assert _HybridSearchAdapter is not None
