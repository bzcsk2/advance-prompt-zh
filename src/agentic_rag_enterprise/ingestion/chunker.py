"""Parent-child chunking for enterprise ingestion.

Ports the upstream ``DocumentChunker`` **algorithm** (heading-aware parent
splitting, small-parent merge, large-parent split, rebalancing, recursive
child splitting) without importing any upstream trust boundary.

Key enterprise differences vs. upstream:
* Parent and child identifiers are **content-addressed and tenant-scoped**
  (``sha256`` of tenant + corpus + document + section path + text). Upstream
  derives parent ids from the source filename stem (``{stem}_p{i}``); that is
  forbidden by the E-007 contract (no filename-derived parent IDs).
* Every chunk carries provenance metadata (``document_id``, ``tenant_id``,
  ``corpus_id``, ``section_path``) so the retrieval path can re-establish
  identity and authorization at read time.
"""

from dataclasses import dataclass, field
from hashlib import sha256

from pydantic import BaseModel

from langchain_text_splitters import (
    MarkdownHeaderTextSplitter,
    RecursiveCharacterTextSplitter,
)

DEFAULT_HEADERS_TO_SPLIT_ON = [("#", "H1"), ("##", "H2"), ("###", "H3")]
DEFAULT_MIN_PARENT_SIZE = 2000
DEFAULT_MAX_PARENT_SIZE = 4000
DEFAULT_CHILD_CHUNK_SIZE = 500
DEFAULT_CHILD_CHUNK_OVERLAP = 100
# 128-bit content-addressed ids (parent/child keys). Longer than upstream's
# filename-derived ids and resistant to collision/guessing.
_PARENT_ID_LEN = 32


class Chunk(BaseModel):
    """Flat chunk produced by the :class:`SimpleChunker` compatibility adapter."""

    chunk_id: str
    parent_id: str | None = None
    text: str
    metadata: dict = {}


class ParentChunk(BaseModel):
    """A heading-bounded parent chunk with provenance metadata."""

    parent_id: str
    document_id: str
    document_version: str
    tenant_id: str
    corpus_id: str
    text: str
    section_path: list[str] = field(default_factory=list)
    metadata: dict[str, object] = field(default_factory=dict)


class ChildChunk(BaseModel):
    """A fine-grained child chunk belonging to a :class:`ParentChunk`."""

    child_id: str
    parent_id: str
    document_id: str
    document_version: str
    tenant_id: str
    corpus_id: str
    text: str
    section_path: list[str] = field(default_factory=list)
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass
class _Seg:
    """Internal segment used while rebalancing parents."""

    text: str
    metadata: dict[str, object] = field(default_factory=dict)


class ParentChildChunker:
    """Heading-aware parent-child chunker (algorithm port of upstream)."""

    def __init__(
        self,
        *,
        min_parent_size: int = DEFAULT_MIN_PARENT_SIZE,
        max_parent_size: int = DEFAULT_MAX_PARENT_SIZE,
        child_chunk_size: int = DEFAULT_CHILD_CHUNK_SIZE,
        child_chunk_overlap: int = DEFAULT_CHILD_CHUNK_OVERLAP,
        headers_to_split_on: list[tuple[str, str]] | None = None,
    ) -> None:
        if min_parent_size <= 0 or max_parent_size < min_parent_size:
            raise ValueError("max_parent_size must be >= min_parent_size > 0")
        if not 0 <= child_chunk_overlap < child_chunk_size:
            raise ValueError("child_chunk_overlap must be in [0, child_chunk_size)")

        self.min_parent_size = min_parent_size
        self.max_parent_size = max_parent_size
        self.child_chunk_size = child_chunk_size
        self.child_chunk_overlap = child_chunk_overlap
        self.headers_to_split_on = headers_to_split_on or DEFAULT_HEADERS_TO_SPLIT_ON

        self._parent_splitter = MarkdownHeaderTextSplitter(
            headers_to_split_on=self.headers_to_split_on, strip_headers=False
        )
        self._child_splitter = RecursiveCharacterTextSplitter(
            chunk_size=child_chunk_size, chunk_overlap=child_chunk_overlap
        )

    def chunk_markdown(
        self,
        text: str,
        *,
        tenant_id: str,
        corpus_id: str,
        document_id: str,
        document_version: str,
        metadata: dict[str, object] | None = None,
    ) -> tuple[list[ParentChunk], list[ChildChunk]]:
        """Split ``text`` into parent + child chunks with stable ids.

        ``document_version`` is required and is part of the content-addressed
        parent/child id, so two versions of the same section get distinct ids
        (the parent store can hold both without one overwriting the other).

        Returns ``(parents, children)`` in deterministic document order.
        """
        raw = self._parent_splitter.split_text(text)
        segs = [_Seg(d.page_content, dict(d.metadata)) for d in raw]

        segs = self._merge_small_parents(segs)
        segs = self._split_large_parents(segs)
        segs = self._clean_small_chunks(segs)

        if any(len(s.text) > self.max_parent_size for s in segs):
            raise ValueError("parent chunk exceeds max_parent_size after rebalancing")

        return self._create_child_chunks(
            segs, tenant_id, corpus_id, document_id, document_version, metadata or {}
        )

    # --- upstream algorithm steps (ported) -------------------------------

    def _merge_small_parents(self, segs: list[_Seg]) -> list[_Seg]:
        merged: list[_Seg] = []
        current: _Seg | None = None
        for seg in segs:
            if current is None:
                current = _Seg(seg.text, dict(seg.metadata))
                continue
            if len(current.text) < self.min_parent_size:
                current.text = current.text + "\n\n" + seg.text
                current.metadata = self._merge_metadata(current.metadata, seg.metadata)
            else:
                merged.append(current)
                current = _Seg(seg.text, dict(seg.metadata))

        if current is not None:
            # Only fold a *small* trailing segment into the previous parent.
            # A trailing segment that already clears MIN must stand alone;
            # otherwise it would be wrongly merged (and later split), losing
            # its distinct section path.
            if len(current.text) < self.min_parent_size and merged:
                last = merged[-1]
                last.text = last.text + "\n\n" + current.text
                last.metadata = self._merge_metadata(last.metadata, current.metadata)
            else:
                merged.append(current)
        return merged

    def _split_large_parents(self, segs: list[_Seg]) -> list[_Seg]:
        out: list[_Seg] = []
        splitter = RecursiveCharacterTextSplitter(
            chunk_size=self.max_parent_size, chunk_overlap=self.child_chunk_overlap
        )
        for seg in segs:
            if len(seg.text) > self.max_parent_size:
                for part in splitter.split_text(seg.text):
                    out.append(_Seg(part, dict(seg.metadata)))
            else:
                out.append(seg)
        return out

    def _rebalance_pair(self, first: _Seg, second: _Seg) -> tuple[_Seg, _Seg]:
        """Redistribute two adjacent segments around the midpoint.

        Ports the upstream ``_rebalance_pair``: joins on a separator, then splits
        at the nearest structural boundary (paragraph/line/space) so both sides
        stay <= ``max_parent_size`` and, when there is enough combined text,
        >= ``min_parent_size``. Returns the (possibly unchanged) pair.
        """
        separator = "\n\n"
        combined = first.text.rstrip() + separator + second.text.lstrip()
        lower = max(1, len(combined) - self.max_parent_size)
        upper = min(self.max_parent_size, len(combined) - 1)
        if len(combined) >= 2 * self.min_parent_size:
            lower = max(lower, self.min_parent_size)
            upper = min(upper, len(combined) - self.min_parent_size)
        preferred = min(max(len(combined) // 2, lower), upper)

        split_at = preferred
        for sep in ("\n\n", "\n", " "):
            before = combined.rfind(sep, lower, preferred + 1)
            after = combined.find(sep, preferred, upper + 1)
            if before >= lower:
                split_at = before
                break
            if after != -1:
                split_at = after
                break

        left_text = combined[:split_at].rstrip()
        right_text = combined[split_at:].lstrip()
        if len(combined) >= 2 * self.min_parent_size and (
            len(left_text) < self.min_parent_size or len(right_text) < self.min_parent_size
        ):
            split_at = preferred
            left_text, right_text = combined[:split_at], combined[split_at:]
        if not left_text or not right_text:
            return first, second

        metadata = self._merge_metadata(dict(first.metadata), second.metadata)
        return _Seg(left_text, dict(metadata)), _Seg(right_text, dict(metadata))

    def _clean_small_chunks(self, segs: list[_Seg]) -> list[_Seg]:
        segs = list(segs)
        out: list[_Seg] = []
        n = len(segs)
        for i, seg in enumerate(segs):
            if len(seg.text) >= self.min_parent_size or n == 1:
                out.append(seg)
                continue
            # Fold into the previous parent if it fits within max size (account
            # for the "\n\n" separator we will insert).
            if out:
                prev = out[-1]
                if len(prev.text) + 2 + len(seg.text) <= self.max_parent_size:
                    prev.text = prev.text + "\n\n" + seg.text
                    prev.metadata = self._merge_metadata(prev.metadata, seg.metadata)
                    continue
            # Otherwise prepend to the next parent if it fits.
            if i + 1 < n:
                nxt = segs[i + 1]
                if len(nxt.text) + 2 + len(seg.text) <= self.max_parent_size:
                    nxt.text = seg.text + "\n\n" + nxt.text
                    nxt.metadata = self._merge_metadata(seg.metadata, nxt.metadata)
                    continue
            out.append(seg)

        # Second pass: any segment still below MIN gets rebalanced with a
        # neighbor so orphan small parents are not emitted (upstream behavior).
        for i, seg in enumerate(out):
            if len(seg.text) >= self.min_parent_size or len(out) == 1:
                continue
            if i < len(out) - 1:
                out[i], out[i + 1] = self._rebalance_pair(out[i], out[i + 1])
            else:
                out[i - 1], out[i] = self._rebalance_pair(out[i - 1], out[i])
        return out

    def _create_child_chunks(
        self,
        segs: list[_Seg],
        tenant_id: str,
        corpus_id: str,
        document_id: str,
        document_version: str,
        base_metadata: dict[str, object],
    ) -> tuple[list[ParentChunk], list[ChildChunk]]:
        parents: list[ParentChunk] = []
        children: list[ChildChunk] = []
        for seg in segs:
            section_path = self._section_path(seg.metadata)
            parent_id = self._make_parent_id(
                tenant_id, corpus_id, document_id, document_version, section_path, seg.text
            )
            parents.append(
                ParentChunk(
                    parent_id=parent_id,
                    document_id=document_id,
                    document_version=document_version,
                    tenant_id=tenant_id,
                    corpus_id=corpus_id,
                    text=seg.text,
                    section_path=section_path,
                    metadata={**base_metadata, **seg.metadata},
                )
            )
            for idx, child_text in enumerate(self._child_splitter.split_text(seg.text)):
                children.append(
                    ChildChunk(
                        child_id=self._make_child_id(parent_id, idx, child_text),
                        parent_id=parent_id,
                        document_id=document_id,
                        document_version=document_version,
                        tenant_id=tenant_id,
                        corpus_id=corpus_id,
                        text=child_text,
                        section_path=section_path,
                        metadata={**base_metadata, **seg.metadata},
                    )
                )
        return parents, children

    # --- id + metadata helpers ------------------------------------------

    @staticmethod
    def _section_path(metadata: dict[str, object]) -> list[str]:
        path: list[str] = []
        for level in ("H1", "H2", "H3"):
            value = metadata.get(level)
            if value:
                path.append(str(value))
        return path

    @staticmethod
    def _make_parent_id(
        tenant_id: str,
        corpus_id: str,
        document_id: str,
        document_version: str,
        section_path: list[str],
        text: str,
    ) -> str:
        blob = (
            f"{tenant_id}|{corpus_id}|{document_id}|{document_version}|"
            f"{' > '.join(section_path)}|{text}"
        )
        return sha256(blob.encode("utf-8")).hexdigest()[:_PARENT_ID_LEN]

    @staticmethod
    def _make_child_id(parent_id: str, idx: int, text: str) -> str:
        blob = f"{parent_id}|{idx}|{text}"
        return sha256(blob.encode("utf-8")).hexdigest()[:_PARENT_ID_LEN]

    @staticmethod
    def _merge_metadata(a: dict[str, object], b: dict[str, object]) -> dict[str, object]:
        merged: dict[str, object] = {}
        keys = list(a.keys()) + [k for k in b.keys() if k not in a]
        for key in keys:
            av = a.get(key)
            bv = b.get(key)
            if av is None:
                merged[key] = bv
            elif bv is None or av == bv:
                merged[key] = av
            else:
                merged[key] = f"{av} -> {bv}"
        return merged


class SimpleChunker:
    """Simple text chunker placeholder.

    Retained as a compatibility adapter until E-007 behavior tests pass.
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
