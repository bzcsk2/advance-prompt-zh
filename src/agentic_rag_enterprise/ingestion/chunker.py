from pydantic import BaseModel


class Chunk(BaseModel):
    chunk_id: str
    parent_id: str | None = None
    text: str
    metadata: dict = {}


class SimpleChunker:
    """Simple text chunker placeholder.

    Production chunking should preserve document hierarchy, tables, section
    headings, parent-child relationships, and source metadata.
    """

    def chunk(self, document_id: str, text: str, size: int = 800) -> list[Chunk]:
        chunks: list[Chunk] = []
        for index, start in enumerate(range(0, len(text), size)):
            chunks.append(
                Chunk(
                    chunk_id=f"{document_id}:{index}",
                    parent_id=document_id,
                    text=text[start : start + size],
                    metadata={"document_id": document_id},
                )
            )
        return chunks
