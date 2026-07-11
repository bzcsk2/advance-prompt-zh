"""Hybrid (dense + sparse) child-chunk retrieval — INTERNAL adapter.

This module builds the Qdrant ACL filter from the caller's
:class:`SecurityContext` and runs a hybrid search. It is an internal adapter
used only by
:class:`agentic_rag_enterprise.retrieval.retriever.SecureRetriever`; it is not
exported from the ``retrieval`` package and must not be called directly by
application/tool code. It does **not** validate corpus discoverability (that
gate lives in ``SecureRetriever``) and never reads a model-supplied parent id;
only child hits from the authorized filter are returned.
"""

from typing import Any

from agentic_rag_enterprise.config import settings
from agentic_rag_enterprise.domain.corpus import CorpusConfig
from agentic_rag_enterprise.domain.security import SecurityContext
from agentic_rag_enterprise.retrieval.models import RetrievalHit, as_acl_scope
from agentic_rag_enterprise.security.filter import build_access_filter
from agentic_rag_enterprise.storage.vector_store import (
    DenseEncoder,
    SparseEncoder,
    VectorStore,
)

DEFAULT_SPARSE_NAME = "sparse"


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value]
    return [str(value)]


class _HybridSearchAdapter:
    """Runs authorized hybrid search and returns typed child hits.

    Internal-only: the secure retrieval entry point is
    :class:`~agentic_rag_enterprise.retrieval.retriever.SecureRetriever`.
    """

    def __init__(
        self,
        vector_store: VectorStore,
        *,
        sparse_name: str = DEFAULT_SPARSE_NAME,
        default_top_k: int | None = None,
    ) -> None:
        self._vector_store = vector_store
        self._sparse_name = sparse_name
        self._default_top_k = default_top_k or settings.max_retrieval_top_k

    def search(
        self,
        ctx: SecurityContext,
        corpus: CorpusConfig,
        query: str,
        top_k: int | None,
        *,
        dense_encoder: DenseEncoder,
        sparse_encoder: SparseEncoder,
    ) -> list[RetrievalHit]:
        flt = build_access_filter(ctx, corpus.corpus_id)
        collection = corpus.vector_collection or corpus.corpus_id
        scored = self._vector_store.search(
            collection,
            query,
            filter=flt,
            top_k=top_k or self._default_top_k,
            dense_encoder=dense_encoder,
            sparse_encoder=sparse_encoder,
            sparse_name=self._sparse_name,
        )
        return [self._to_hit(sp.payload, sp.score) for sp in scored]

    @staticmethod
    def _to_hit(payload: dict[str, Any], score: float) -> RetrievalHit:
        return RetrievalHit(
            chunk_id=str(payload.get("chunk_id", "")),
            parent_id=str(payload.get("parent_id", "")),
            document_id=str(payload.get("document_id", "")),
            document_version=str(payload.get("document_version", "")),
            corpus_id=str(payload.get("corpus_id", "")),
            tenant_id=str(payload.get("tenant_id", "")),
            text=str(payload.get("text", "")),
            score=float(score),
            section_path=_as_list(payload.get("section_path")),
            status=str(payload.get("status", "active")),
            deprecated=bool(payload.get("deprecated", False)),
            security_level=str(payload.get("security_level", "public")),
            acl_scope=as_acl_scope(payload.get("acl_scope", "restricted")),
            allowed_user_ids=_as_list(payload.get("allowed_user_ids")),
            allowed_group_ids=_as_list(payload.get("allowed_group_ids")),
            denied_user_ids=_as_list(payload.get("denied_user_ids")),
            denied_group_ids=_as_list(payload.get("denied_group_ids")),
        )
