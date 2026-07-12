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
        """Raw, untrusted lookup. No authorization is performed here.

        Internal accessor only. The retrieval/API surface MUST NOT call this
        directly (build plan §12.5: no reading a parent by a model-supplied id);
        the sole authorized retrieval accessor is
        :class:`~agentic_rag_enterprise.retrieval.parent_reader.ParentReader`.
        Enforced by ``tests/unit/test_retrieval_boundary.py``. (Ingestion's
        verify/publish steps are a trusted control-plane caller.)
        """
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

    def deprecate_document(self, document_id: str, document_version: str) -> None:
        """Logical-delete every parent of one (document, version). Idempotent."""
        for chunk in list(self._store.values()):
            if chunk.document_id == document_id and chunk.document_version == document_version:
                self.deprecate(chunk.parent_id)

    def delete_document(self, document_id: str, document_version: str) -> None:
        """Physical-purge every parent of one (document, version). Idempotent."""
        for chunk in list(self._store.values()):
            if chunk.document_id == document_id and chunk.document_version == document_version:
                self.delete(chunk.parent_id)

    def update_acl_document(
        self, document_id: str, document_version: str, acl_fields: dict[str, object]
    ) -> None:
        """Patch ACL metadata on every parent of one (document, version).

        No content/vector change; used by ACL tightening (§10.7).
        """
        for chunk in list(self._store.values()):
            if chunk.document_id == document_id and chunk.document_version == document_version:
                metadata = dict(chunk.metadata)
                metadata.update(acl_fields)
                self._store[chunk.parent_id] = chunk.model_copy(update={"metadata": metadata})

    def __contains__(self, parent_id: str) -> bool:
        return parent_id in self._store
