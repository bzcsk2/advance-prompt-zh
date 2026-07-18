"""E-014 dependency composition (build plan §6: ``services/``).

Thin assemblers that wire the storage stack, model provider, and corpus resolver
into a :class:`ChatService`. The core assembler (:func:`build_chat_service`)
takes already-constructed dependencies so it is fully unit-testable with fakes.
:func:`build_chat_service_from_settings` is the best-effort deployment assembly
that reads :mod:`agentic_rag_enterprise.config`; real embedding encoders and
non-fake model providers are deferred (Internal MVP runs locally / controlled
with the ``fake`` provider).
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

import yaml

from agentic_rag_enterprise.config import settings
from agentic_rag_enterprise.domain.corpus import CorpusConfig
from agentic_rag_enterprise.providers import ModelProfile, ModelProvider, create_provider
from agentic_rag_enterprise.retrieval.hybrid import _HybridSearchAdapter
from agentic_rag_enterprise.retrieval.parent_reader import ParentReader
from agentic_rag_enterprise.retrieval.retriever import SecureRetriever
from agentic_rag_enterprise.services.chat_service import ChatService, ChatServiceError
from agentic_rag_enterprise.storage.metadata_store import MetadataStore
from agentic_rag_enterprise.storage.parent_store import ParentStore
from agentic_rag_enterprise.storage.vector_store import (
    DenseEncoder,
    SparseEncoder,
    VectorStore,
)


def build_chat_service(
    *,
    retriever: SecureRetriever,
    dense_encoder: DenseEncoder,
    sparse_encoder: SparseEncoder,
    model: ModelProvider,
    resolve_corpus: Callable[[str], CorpusConfig],
    top_k: int | None = None,
) -> ChatService:
    """Assemble a ChatService from already-constructed dependencies."""
    return ChatService(
        retriever=retriever,
        dense_encoder=dense_encoder,
        sparse_encoder=sparse_encoder,
        model=model,
        resolve_corpus=resolve_corpus,
        top_k=top_k,
    )


def resolve_corpus_from_yaml(path: str | Path) -> Callable[[str], CorpusConfig]:
    """Build a ``corpus_id -> domain.CorpusConfig`` resolver from a YAML file.

    Uses the **domain** ``CorpusConfig`` (the type ``run_fast_path`` /
    ``SecureRetriever`` require), NOT the M0 baseline ``schemas.CorpusConfig``.
    """
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    corpora = {c["corpus_id"]: CorpusConfig(**c) for c in data.get("corpora", [])}
    if not corpora:
        raise ChatServiceError(f"no corpora defined in {path}")

    def _resolve(corpus_id: str) -> CorpusConfig:
        try:
            return corpora[corpus_id]
        except KeyError as exc:
            raise ChatServiceError(f"unknown corpus_id: {corpus_id!r}") from exc

    return _resolve


def build_default_model() -> ModelProvider:
    """Build a model provider from runtime settings."""
    provider_name = settings.llm_provider
    if provider_name in ("zen", "kilo"):
        default_models = {
            "zen": "deepseek-v4-flash-free",
            "kilo": "nvidia/nemotron-3-super-120b-a12b:free",
        }
        model = settings.llm_model or default_models.get(provider_name, "default")
        return create_provider(
            ModelProfile(provider=provider_name, model=model, purpose="synthesis")
        )
    # Fall back to fake provider for testing / development.
    return create_provider(ModelProfile(provider="fake", model="fake-model", purpose="synthesis"))


def build_chat_service_from_settings(
    *,
    dense_encoder: DenseEncoder | None = None,
    sparse_encoder: SparseEncoder | None = None,
    corpus_yaml: str | Path | None = None,
    top_k: int | None = None,
) -> ChatService:
    """Best-effort deployment assembly from runtime settings.

    Real embedding encoders and non-fake model providers are deferred; callers
    inject the encoders. If absent, a clear :class:`ChatServiceError` explains
    the gap instead of failing open.
    """
    if dense_encoder is None or sparse_encoder is None:
        raise ChatServiceError(
            "embedding encoders are not configured; the Internal MVP wires the "
            "fake/test encoders via the deployment composition (real encoders "
            "and non-fake model providers are deferred)."
        )

    from qdrant_client import QdrantClient  # lazy: only needed for real serving

    mstore = MetadataStore(settings.metadata_db_path)
    vstore = VectorStore(QdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key))
    pstore = ParentStore()
    retriever = SecureRetriever(
        _HybridSearchAdapter(vstore),
        ParentReader(pstore),
        metadata_store=mstore,
    )
    resolver = resolve_corpus_from_yaml(
        corpus_yaml or Path(__file__).resolve().parents[3] / "configs" / "corpora.yaml"
    )
    return build_chat_service(
        retriever=retriever,
        dense_encoder=dense_encoder,
        sparse_encoder=sparse_encoder,
        model=build_default_model(),
        resolve_corpus=resolver,
        top_k=top_k or settings.max_retrieval_top_k,
    )
