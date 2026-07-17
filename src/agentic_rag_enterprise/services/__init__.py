"""E-014 application service package (build plan §6: ``services/``)."""

from agentic_rag_enterprise.services.claims_schema import ClaimExtraction
from agentic_rag_enterprise.services.chat_service import (
    ChatService,
    ChatServiceError,
    ModelInvocationError,
)
from agentic_rag_enterprise.services.composition import (
    build_chat_service,
    build_chat_service_from_settings,
    build_default_model,
    resolve_corpus_from_yaml,
)
from agentic_rag_enterprise.services.container import (
    DENSE_DIM,
    DefaultServiceContainer,
    get_default_container,
)

__all__ = [
    "ChatService",
    "ChatServiceError",
    "ModelInvocationError",
    "ClaimExtraction",
    "build_chat_service",
    "build_chat_service_from_settings",
    "build_default_model",
    "resolve_corpus_from_yaml",
    "DefaultServiceContainer",
    "get_default_container",
    "DENSE_DIM",
]
