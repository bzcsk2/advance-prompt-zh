"""Retrieval package.

The only supported public entry point for retrieval is
:class:`SecureRetriever`. The hybrid search adapter is internal (private) and
is deliberately not exported here, so application/tool code cannot bypass the
corpus-discoverability gate and parent second-authorization by calling it
directly.
"""

__all__ = ["Retriever", "SecureRetriever"]
