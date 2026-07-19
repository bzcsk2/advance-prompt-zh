"""Shared, in-process default service container (Internal MVP runnable default).

The default ``POST /v1/chat`` and the Gradio adapter are backed by ONE
process-wide :class:`ChatService` that shares a single storage stack — Qdrant in
memory, the parent store, and the metadata store — so a document ingested
through :meth:`DefaultServiceContainer.ingest` is immediately retrievable by the
chat service **without any external dependency** (no Qdrant server, no real
encoders, no real LLM).

This makes the default application runnable out of the box, which closes the
E-014 acceptance gap where the previously-default assembly raised on missing
encoders and the ``FakeModel`` had no ``ClaimExtraction`` registered (so a
``sufficient`` request could never succeed). The storage stack is also shared
with the ingestion pipeline so the full run-chain (ingest -> retrieve ->
answer/abstain) is exercisable end-to-end against the *real* default app.
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from pydantic import BaseModel
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, SparseVector

from agentic_rag_enterprise.answer import Claim
from agentic_rag_enterprise.config import settings
from agentic_rag_enterprise.ingestion.chunker import ParentChildChunker
from agentic_rag_enterprise.ingestion.job import DocumentManager, IngestionRequest, IngestionResult
from agentic_rag_enterprise.providers import ModelProfile
from agentic_rag_enterprise.domain.corpus import CorpusConfig
from agentic_rag_enterprise.corpus.registry import InMemoryCorpusRegistry
from agentic_rag_enterprise.retrieval.hybrid import _HybridSearchAdapter
from agentic_rag_enterprise.retrieval.parent_reader import ParentReader
from agentic_rag_enterprise.retrieval.retriever import SecureRetriever
from agentic_rag_enterprise.security.policy import ResourceAcl
from agentic_rag_enterprise.services.claims_schema import ClaimExtraction
from agentic_rag_enterprise.services.chat_service import ChatService
from agentic_rag_enterprise.services.composition import resolve_corpus_from_yaml
from agentic_rag_enterprise.storage.metadata_store import MetadataStore
from agentic_rag_enterprise.storage.parent_store import ParentStore
from agentic_rag_enterprise.storage.vector_store import VectorStore

# Deterministic dev encoder dimension (must match the Qdrant collection size).
DENSE_DIM = 4

# Marker the chat service writes around each evidence id in the synthesis prompt.
_EVID_MARKER = re.compile(r"\[([^\]]+)\]")


# --------------------------------------------------------------------------- #
# Deterministic, dependency-free dev encoders (mirror tests/fixtures semantics)
# --------------------------------------------------------------------------- #
class _DevDenseEncoder:
    """Deterministic dense encoder (hash -> fixed-dim vector)."""

    def __call__(self, text: str) -> list[float]:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        return [float(b) / 255.0 for b in digest[:DENSE_DIM]]


class _DevSparseEncoder:
    """Deterministic sparse encoder (word hashes -> indices/values)."""

    def __call__(self, text: str) -> SparseVector:
        indices: list[int] = []
        values: list[float] = []
        words = sorted({w for w in text.split() if w})[:8]
        for word in words:
            idx = int(hashlib.sha256(word.encode("utf-8")).hexdigest(), 16) % 1000
            indices.append(idx)
            values.append(1.0)
        return SparseVector(indices=indices, values=values)


# --------------------------------------------------------------------------- #
# Hermetic synthesis model for the Internal-MVP default
# --------------------------------------------------------------------------- #
class _DevSynthesisModel:
    """Deterministic, hermetic synthesis model (no external service).

    Implements the project's ``ModelProvider`` protocol. For claim extraction it
    reads the ``[evidence_id]`` markers the chat service writes into the prompt
    and emits one claim per evidence id that cites it, so every claim resolves to
    a real snapshot (build plan §16.3). This makes a ``sufficient`` request
    produce a valid, fail-closed ``AnswerEnvelope`` with no real LLM.

    The model is also fail-closed: it never invents an evidence id, and it never
    sees the security context (the prompt carries only query + evidence).
    """

    def __init__(self) -> None:
        self.profile = ModelProfile(provider="fake", model="dev-default", purpose="synthesis")

    def invoke(self, messages: list[dict[str, str]], **kwargs: Any) -> str:
        return "Dev default synthesis answer."

    def with_structured_output(
        self, schema: type[BaseModel], **kwargs: Any
    ) -> "_DevStructuredWrapper":
        return _DevStructuredWrapper(self, schema)


class _DevStructuredWrapper:
    """Structured-output wrapper returned by ``with_structured_output``."""

    def __init__(self, model: "_DevSynthesisModel", schema: type[BaseModel]) -> None:
        self._model = model
        self._schema = schema

    def invoke(self, messages: list[dict[str, str]], **kwargs: Any) -> BaseModel:
        user = ""
        for msg in reversed(messages):
            if msg.get("role") == "user":
                user = msg.get("content", "")
                break
        evidence_ids = _EVID_MARKER.findall(user)
        claims = [
            Claim(
                claim_id=f"claim-{i}",
                text=f"Evidence {eid} supports the request.",
                evidence_ids=(eid,),
            )
            for i, eid in enumerate(evidence_ids)
        ]
        return ClaimExtraction(draft_answer="Based on the authorized evidence.", claims=claims)


# --------------------------------------------------------------------------- #
# Container
# --------------------------------------------------------------------------- #
class DefaultServiceContainer:
    """Holds the single shared storage stack + ChatService for the Internal MVP."""

    def __init__(self) -> None:
        # In-memory Qdrant: no server required.
        self._client = QdrantClient(location=":memory:")
        self._vstore = VectorStore(self._client)
        self._pstore = ParentStore()

        # Metadata DB on a throwaway sqlite file (no shared server required).
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(path)
        self._mstore = MetadataStore(path)

        self._dense = _DevDenseEncoder()
        self._sparse = _DevSparseEncoder()
        self._model = _DevSynthesisModel()

        corpora_path = Path(__file__).resolve().parents[3] / "configs" / "corpora.yaml"
        # Register every corpus in the control-plane registry before any ingest,
        # because `documents` has a foreign key to `corpus_registry`.
        import yaml

        _corpora_data = yaml.safe_load(corpora_path.read_text(encoding="utf-8")) or {}
        _corpora = [CorpusConfig(**_c) for _c in _corpora_data.get("corpora", [])]
        for _c in _corpora:
            self._mstore.register_corpus(_c)

        self._registry = InMemoryCorpusRegistry(_corpora)
        self._resolver = resolve_corpus_from_yaml(corpora_path)

        self._manager = DocumentManager(
            metadata_store=self._mstore,
            vector_store=self._vstore,
            parent_store=self._pstore,
            chunker=ParentChildChunker(),
            dense_encoder=self._dense,
            sparse_encoder=self._sparse,
            corpus_registry=self._registry,
        )

        self._service = ChatService(
            retriever=SecureRetriever(
                _HybridSearchAdapter(self._vstore),
                ParentReader(self._pstore),
                metadata_store=self._mstore,
            ),
            dense_encoder=self._dense,
            sparse_encoder=self._sparse,
            model=self._model,
            resolve_corpus=self._resolver,
            top_k=settings.max_retrieval_top_k,
        )

    def ingest(
        self,
        *,
        tenant_id: str,
        corpus_id: str,
        document_id: str,
        document_version: str,
        content: str,
        acl: ResourceAcl,
        job_id: str,
        **kwargs: Any,
    ) -> IngestionResult:
        """Ingest one (document, version) into the SHARED storage stack.

        After this returns ``INDEXED``, the document is immediately retrievable
        by :attr:`service` because the chat service uses the same stores.
        """
        # Ensure the corpus collection exists (idempotent).
        self._vstore.create_collection(corpus_id, DENSE_DIM, distance=Distance.COSINE)
        request = IngestionRequest(
            tenant_id=tenant_id,
            corpus_id=corpus_id,
            document_id=document_id,
            document_version=document_version,
            content=content,
            acl=acl,
            job_id=job_id,
            **kwargs,
        )
        return self._manager.ingest(request)

    @property
    def service(self) -> ChatService:
        return self._service

    @property
    def metadata_store(self) -> MetadataStore:
        return self._mstore

    @property
    def vector_store(self) -> VectorStore:
        return self._vstore

    @property
    def parent_store(self) -> ParentStore:
        return self._pstore

    @property
    def corpus_registry(self) -> InMemoryCorpusRegistry:
        return self._registry

    @property
    def document_manager(self) -> DocumentManager:
        return self._manager


_CONTAINER: DefaultServiceContainer | None = None


def get_default_container() -> DefaultServiceContainer:
    """Return the process-wide default service container (lazily built)."""
    global _CONTAINER
    if _CONTAINER is None:
        _CONTAINER = DefaultServiceContainer()
    return _CONTAINER


def reset_default_container() -> None:
    """Discard the process-wide default container.

    The next :func:`get_default_container` call rebuilds a fresh, empty storage
    stack. Used by the end-to-end default-app tests to get a hermetic
    in-memory Qdrant + sqlite (no pollution from other tests that share the
    singleton), without altering any other test module's view of the singleton.
    """
    global _CONTAINER
    _CONTAINER = None
