"""Parent chunk store.

This store performs **no authorization**. It is a raw, untrusted read/write
layer keyed by parent id. All authorization (tenant/corpus/document identity,
version, lifecycle, and ACL) happens in
:mod:`agentic_rag_enterprise.retrieval.parent_reader`, which loads from here
and then verifies before returning an :class:`AuthorizedParent`.

Exposing a bare ``load_parent(parent_id)`` as a public, authorized entry point
is explicitly forbidden by the E-007 contract: model- or tool-supplied parent
ids must never bypass the second authorization pass.
"""

from agentic_rag_enterprise.ingestion.chunker import ParentChunk


class ParentStore:
    """In-memory (optionally JSON-backed) parent chunk store."""

    def __init__(self) -> None:
        self._store: dict[str, ParentChunk] = {}

    def put(self, chunk: ParentChunk) -> None:
        self._store[chunk.parent_id] = chunk

    def get(self, parent_id: str) -> ParentChunk | None:
        """Raw, untrusted lookup. No authorization is performed here."""
        return self._store.get(parent_id)

    def delete(self, parent_id: str) -> None:
        """Remove a parent (used by ingestion compensation). Unauthorized."""
        self._store.pop(parent_id, None)

    def deprecate(self, parent_id: str) -> None:
        """Mark a stored parent inactive so retrieval's second-auth excludes it.

        The Parent Store is raw/untrusted; ``ParentReader`` fails closed unless
        ``status == "active"`` and ``deprecated is False``, so flipping either is
        sufficient to make the parent invisible (build plan §10.4 / §10.10 #5).
        """
        chunk = self._store.get(parent_id)
        if chunk is None:
            return
        metadata = dict(chunk.metadata)
        metadata["status"] = "inactive"
        metadata["deprecated"] = True
        self._store[parent_id] = chunk.model_copy(update={"metadata": metadata})

    def __contains__(self, parent_id: str) -> bool:
        return parent_id in self._store
