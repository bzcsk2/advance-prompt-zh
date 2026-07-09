from agentic_rag_enterprise.schemas import Evidence


class Retriever:
    """Retrieval interface.

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
