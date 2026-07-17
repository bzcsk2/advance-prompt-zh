"""Architecture test: only SecureRetriever is the public retrieval entry point.

The hybrid search adapter must stay internal (private, not exported from the
``retrieval`` package) so callers cannot bypass the corpus-discoverability
gate and parent second-authorization by calling it directly (P2-1).
"""

import ast
from pathlib import Path

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


# --- E-009: no un-authorized direct parent read on the retrieval/API path ---

_PKG_ROOT = Path(retrieval_pkg.__file__).resolve().parent.parent
_GUARDED_FILES = [
    _PKG_ROOT / "retrieval" / "retriever.py",
    _PKG_ROOT / "api" / "main.py",
]


@pytest.mark.parametrize("path", _GUARDED_FILES, ids=lambda p: p.name)
def test_retrieval_api_surface_never_calls_parent_store_get(path: Path) -> None:
    """§12.5: the retrieval/API surface must load parents ONLY via ParentReader.

    A model-/tool-supplied ``parent_id`` must never reach ``ParentStore.get``
    directly on the read path; ``ParentReader.load_parent_for_hit`` is the sole
    authorized accessor.
    """
    tree = ast.parse(path.read_text(), filename=str(path))

    # No ``from ...storage.parent_store import ParentStore`` on these modules.
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            assert "storage.parent_store" not in node.module, (
                f"{path.name} must not import ParentStore (use ParentReader)"
            )

    # No ``<something>.get(...)`` where the receiver is a parent store binding.
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == "get":
            receiver = node.value
            name = getattr(receiver, "attr", getattr(receiver, "id", ""))
            assert "parent" not in name.lower() or "reader" in name.lower(), (
                f"{path.name} calls .get on a parent-store-like binding "
                f"({name!r}); parents must load via ParentReader"
            )
