"""Retrieval package.

The only supported public entry point for retrieval is
:class:`SecureRetriever`. The hybrid search adapter is internal (private) and
is deliberately not exported here, so application/tool code cannot bypass the
corpus-discoverability gate and parent second-authorization by calling it
directly.
"""

from agentic_rag_enterprise.retrieval.deduplication import Deduplicator
from agentic_rag_enterprise.retrieval.evidence import EvidenceBuilder
from agentic_rag_enterprise.retrieval.retriever import Retriever, SecureRetriever

__all__ = ["Retriever", "SecureRetriever", "Deduplicator", "EvidenceBuilder"]
