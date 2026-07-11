"""Retrieval entry points.

``Retriever`` is the M0 baseline mock (kept green for characterization tests).
``SecureRetriever`` is the E-007 enterprise path: it enforces the **corpus
discoverability** precondition before any document ACL filter is built, then
runs authorized hybrid child retrieval and a second parent-authorization pass.
"""

from agentic_rag_enterprise.config import settings
from agentic_rag_enterprise.domain.corpus import CorpusConfig
from agentic_rag_enterprise.domain.security import SecurityContext
from agentic_rag_enterprise.retrieval.hybrid import _HybridSearchAdapter
from agentic_rag_enterprise.retrieval.models import (
    CorpusNotDiscoverableError,
    ParentAuthorizationError,
    RetrievalResult,
)
from agentic_rag_enterprise.retrieval.parent_reader import ParentReader
from agentic_rag_enterprise.schemas import Evidence
from agentic_rag_enterprise.security.filter import EmptyAuthorizationScopeError
from agentic_rag_enterprise.security.policy import can_discover_corpus
from agentic_rag_enterprise.storage.metadata_store import MetadataStore
from agentic_rag_enterprise.storage.vector_store import DenseEncoder, SparseEncoder


class Retriever:
    """Retrieval interface (M0 baseline mock).

    Replace the mock implementation with Qdrant hybrid search, payload filters,
    parent-child chunk retrieval, and reranking.
    """

    def retrieve(self, query: str, corpus_ids: list[str], top_k: int = 8) -> list[Evidence]:
        if not corpus_ids:
            corpus_ids = ["default"]

        return [
            Evidence(
                evidence_id="mock-evidence-1",
                corpus_id=corpus_ids[0],
                document_id="mock-doc",
                chunk_id="mock-chunk",
                text=f"Mock evidence for query: {query}",
                score=1.0,
                metadata={"retriever": "mock"},
            )
        ][:top_k]


class SecureRetriever:
    """Enterprise retrieval with corpus discoverability + parent 2nd auth.

    A ``metadata_store`` is mandatory: it supplies the control-plane
    active-version gate (build plan §10.10 #5). A child hit is dropped unless its
    ``document_version`` equals the Metadata DB's current active version for that
    document, so a freshly-committed active version is the only one that can reach
    the model even if the old version's Qdrant points have not yet been cleaned up
    on the data plane. Making it required (not optional) removes the fail-open
    bypass where retrieval could run with no gate at all (E-008.2 P1-7).
    """

    def __init__(
        self,
        hybrid: _HybridSearchAdapter,
        parent_reader: ParentReader,
        *,
        default_top_k: int | None = None,
        metadata_store: MetadataStore,
    ) -> None:
        self._hybrid = hybrid
        self._parent_reader = parent_reader
        self._default_top_k = default_top_k or settings.max_retrieval_top_k
        self._metadata_store = metadata_store

    def validate_corpus(self, ctx: SecurityContext, corpus: CorpusConfig) -> None:
        """Raise :class:`CorpusNotDiscoverableError` if the corpus gate fails."""
        if corpus.tenant_id != ctx.tenant_id:
            raise CorpusNotDiscoverableError(
                f"corpus {corpus.corpus_id} belongs to tenant {corpus.tenant_id}, "
                f"not {ctx.tenant_id}"
            )
        if not corpus.enabled:
            raise CorpusNotDiscoverableError(f"corpus {corpus.corpus_id} is disabled")
        if not corpus.searchable:
            raise CorpusNotDiscoverableError(f"corpus {corpus.corpus_id} is not searchable")
        if not can_discover_corpus(ctx, corpus.corpus_id):
            raise CorpusNotDiscoverableError(
                f"corpus {corpus.corpus_id} is not discoverable for this context"
            )

    def retrieve(
        self,
        ctx: SecurityContext,
        query: str,
        corpus: CorpusConfig,
        top_k: int | None = None,
        *,
        dense_encoder: DenseEncoder,
        sparse_encoder: SparseEncoder,
    ) -> RetrievalResult:
        """Run the locked secure-retrieval flow and return typed results.

        Fails closed: any corpus-gate failure yields an empty
        :class:`RetrievalResult` rather than broadening access.
        """
        if top_k is None:
            top_k = self._default_top_k

        try:
            self.validate_corpus(ctx, corpus)
        except CorpusNotDiscoverableError:
            return RetrievalResult(hits=[], denied_parent_count=0)

        # An empty authorization scope (e.g. no allowed_security_levels) makes
        # the PDP deny everything; the PEP raises instead of broadening access.
        try:
            child_hits = self._hybrid.search(
                ctx,
                corpus,
                query,
                top_k,
                dense_encoder=dense_encoder,
                sparse_encoder=sparse_encoder,
            )
        except EmptyAuthorizationScopeError:
            return RetrievalResult(hits=[], denied_parent_count=0)

        hits: list[tuple] = []
        denied_count = 0
        for hit in child_hits:
            # Control-plane active-version gate (build plan §10.10 #5): drop
            # hits whose version is not the Metadata DB's current active version
            # for that document, so a stale-but-not-yet-cleaned version cannot
            # enter the parent/model path after the active version has switched.
            active_version = self._metadata_store.get_active_version(
                hit.tenant_id, hit.corpus_id, hit.document_id
            )
            if active_version is None or hit.document_version != active_version:
                continue
            try:
                parent = self._parent_reader.load_parent_for_hit(hit, ctx)
            except ParentAuthorizationError:
                # A parent that fails the second-auth pass is simply not
                # returned. Storage faults / programming errors are NOT masked
                # as authorization denials and propagate for explicit handling.
                denied_count += 1
                continue
            hits.append((hit, parent))

        return RetrievalResult(hits=hits, denied_parent_count=denied_count)
