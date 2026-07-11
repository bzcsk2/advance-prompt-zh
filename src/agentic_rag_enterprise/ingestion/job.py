"""Idempotent ingestion Job and active-version protocol (build plan §10).

The job wraps the E-007 ported ingestion chain — ``ParentChildChunker`` →
``ParentStore`` → Qdrant ``VectorStore`` — and adds the control-plane required
by §10.4 (idempotency), §10.5 (compensation) and §10.10 (cross-store
consistency). Metadata DB (``MetadataStore``) is the single source of truth for
lifecycle and active version; Qdrant / Parent Store / filesystem are rebuildable
data planes.

Steps are reentrant and recorded as step markers so a crashed/interrupted job
can resume without producing duplicate business IDs or Chunks (§10.10 #3).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from agentic_rag_enterprise.domain.chunk import ChunkRecord
from agentic_rag_enterprise.domain.document import SourceDocument
from agentic_rag_enterprise.domain.ingestion import DocumentStatus, JobStatus
from agentic_rag_enterprise.ingestion.chunker import ChildChunk, ParentChildChunker, ParentChunk
from agentic_rag_enterprise.security.policy import ResourceAcl
from agentic_rag_enterprise.storage.metadata_store import ActiveVersionConflict, MetadataStore
from agentic_rag_enterprise.storage.parent_store import ParentStore
from agentic_rag_enterprise.storage.vector_store import (
    DenseEncoder,
    SparseEncoder,
    VectorStore,
    child_chunk_to_point,
    child_point_id,
)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


class IngestionStatus(str, Enum):
    INDEXED = "indexed"
    ALREADY_INDEXED = "already_indexed"
    FAILED = "failed"


@dataclass
class IngestionRequest:
    """All inputs needed to ingest a single (document, version)."""

    tenant_id: str
    corpus_id: str
    document_id: str
    document_version: str
    content: str
    acl: ResourceAcl
    job_id: str

    title: str = ""
    source_uri: str = ""
    source_connector: str = "file"
    source_native_id: Optional[str] = None
    source_filename: str = ""
    mime_type: str = "text/markdown"
    acl_policy_id: str = "default"
    parser_name: str = "markdown"
    parser_version: str = "1.0"
    chunking_version: str = "1.0"
    embedding_model: str = "fake"
    embedding_version: str = "1.0"
    authority_level: int = 50
    security_level: str = "internal"


@dataclass
class IngestionResult:
    status: IngestionStatus
    job_id: str
    document_version: str
    parent_count: int = 0
    child_count: int = 0
    error_code: Optional[str] = None
    error_message: Optional[str] = None


class IngestionJob:
    """Reentrant, step-marked ingestion job implementing the active-version protocol."""

    STEPS = [
        "acquire",
        "parse",
        "chunk",
        "write_parents",
        "write_qdrant",
        "commit",
        "publish",
        "finalize",
    ]

    def __init__(
        self,
        *,
        store: MetadataStore,
        vector_store: VectorStore,
        parent_store: ParentStore,
        chunker: ParentChildChunker,
        dense_encoder: DenseEncoder,
        sparse_encoder: SparseEncoder,
        request: IngestionRequest,
    ) -> None:
        self._store = store
        self._vector = vector_store
        self._parents = parent_store
        self._chunker = chunker
        self._dense = dense_encoder
        self._sparse = sparse_encoder
        self._req = request

        # Validate tenant binding up front (complements the mapper's guard).
        if request.acl.tenant_id != request.tenant_id:
            raise ValueError(
                f"ACL tenant {request.acl.tenant_id!r} != document tenant {request.tenant_id!r}"
            )

        self._parents_list: list[ParentChunk] = []
        self._children_list: list[ChildChunk] = []
        self._source_doc: Optional[SourceDocument] = None
        self._raw_hash: str = ""
        self._parsed_hash: str = ""

    # ------------------------------------------------------------------ #
    # Public entry point
    # ------------------------------------------------------------------ #
    def run(self, max_step: Optional[str] = None) -> IngestionResult:
        """Run steps up to and including ``max_step`` (None = all).

        A ``max_step`` shorter than the full pipeline simulates a crash/interrupt;
        a subsequent ``run()`` resumes from the last completed step marker.
        """
        steps = self.STEPS
        if max_step is not None:
            if max_step not in steps:
                raise ValueError(f"unknown step: {max_step}")
            steps = steps[: steps.index(max_step) + 1]

        # Idempotency guard: if THIS job already succeeded for an active
        # version, return ALREADY_INDEXED without rework. A job that committed
        # but has not finalized (crashed between commit and finalize) is NOT
        # "succeeded", so a resume re-runs publish + finalize (build plan
        # §10.10 #3, #6, #7).
        existing = self._store.get_document(
            self._req.tenant_id,
            self._req.corpus_id,
            self._req.document_id,
            self._req.document_version,
        )
        if (
            existing is not None
            and existing.status == DocumentStatus.ACTIVE
            and self._store.get_job_status(self._req.job_id) == JobStatus.SUCCEEDED
        ):
            return IngestionResult(
                status=IngestionStatus.ALREADY_INDEXED,
                job_id=self._req.job_id,
                document_version=self._req.document_version,
            )

        try:
            for step in steps:
                if self._store.is_step_done(self._req.job_id, step):
                    continue
                getattr(self, f"_step_{step}")()
                self._store.mark_step(self._req.job_id, step, "done")

            return IngestionResult(
                status=IngestionStatus.INDEXED,
                job_id=self._req.job_id,
                document_version=self._req.document_version,
                parent_count=len(self._parents_list),
                child_count=len(self._children_list),
            )
        except ActiveVersionConflict as exc:
            self._store.mark_job_terminal(
                self._req.job_id,
                JobStatus.FAILED,
                error_code="active_version_conflict",
                error_message=str(exc),
            )
            return IngestionResult(
                status=IngestionStatus.FAILED,
                job_id=self._req.job_id,
                document_version=self._req.document_version,
                error_code="active_version_conflict",
                error_message=str(exc),
            )
        except Exception as exc:  # noqa: BLE001 — fail closed + compensate
            if not self._store.is_step_done(self._req.job_id, "commit"):
                self._compensate()
            self._store.mark_job_terminal(
                self._req.job_id,
                JobStatus.FAILED,
                error_code="ingestion_error",
                error_message=str(exc),
            )
            return IngestionResult(
                status=IngestionStatus.FAILED,
                job_id=self._req.job_id,
                document_version=self._req.document_version,
                error_code="ingestion_error",
                error_message=str(exc),
            )

    # ------------------------------------------------------------------ #
    # Steps
    # ------------------------------------------------------------------ #
    def _step_acquire(self) -> None:
        # Create the document control-plane row FIRST so the ingestion_jobs FK
        # (document_id, tenant_id, corpus_id, document_version) is satisfied.
        self._raw_hash = _sha256(self._req.content)
        self._source_doc = self._build_source_document(status=DocumentStatus.PROCESSING)
        self._store.upsert_document(self._source_doc)
        self._store.acquire_job(
            job_id=self._req.job_id,
            document_id=self._req.document_id,
            document_version=self._req.document_version,
            corpus_id=self._req.corpus_id,
            tenant_id=self._req.tenant_id,
            parser_version=self._req.parser_version,
            chunking_version=self._req.chunking_version,
            embedding_version=self._req.embedding_version,
            raw_hash=self._raw_hash,
        )

    def _step_parse(self) -> None:
        # Content already hashed in acquire; parsed_hash is refined after chunking.
        self._parsed_hash = _sha256(self._req.content)

    def _ensure_chunked(self) -> None:
        """Chunk lazily and idempotently.

        Chunking is deterministic for a fixed ``(content, version)``, so on a
        resumed run (where in-memory state was lost) re-chunking reproduces the
        exact same content-addressed parent/child ids. Upserts downstream are
        therefore idempotent and never create duplicate business artifacts
        (build plan §10.4 / §10.10 #3).
        """
        if self._children_list:
            return
        parents, children = self._chunker.chunk_markdown(
            self._req.content,
            tenant_id=self._req.tenant_id,
            corpus_id=self._req.corpus_id,
            document_id=self._req.document_id,
            document_version=self._req.document_version,
        )
        self._parents_list = parents
        self._children_list = children
        self._parsed_hash = _sha256("".join(c.text for c in children))

    def _step_chunk(self) -> None:
        self._ensure_chunked()

    def _step_write_parents(self) -> None:
        self._ensure_chunked()
        auth = self._auth_metadata()
        for parent in self._parents_list:
            self._parents.put(
                ParentChunk(
                    parent_id=parent.parent_id,
                    document_id=parent.document_id,
                    document_version=parent.document_version,
                    tenant_id=parent.tenant_id,
                    corpus_id=parent.corpus_id,
                    text=parent.text,
                    section_path=parent.section_path,
                    metadata={**parent.metadata, **auth},
                )
            )

    def _step_write_qdrant(self) -> None:
        self._ensure_chunked()
        collection = self._req.corpus_id
        acl = self._req.acl
        points = [
            child_chunk_to_point(
                child,
                acl,
                status="processing",
                deprecated=False,
                dense_encoder=self._dense,
                sparse_encoder=self._sparse,
            )
            for child in self._children_list
        ]
        self._vector.upsert(collection, points)

        for child in self._children_list:
            self._store.upsert_chunk_record(
                self._make_chunk_record(
                    chunk_id=child.child_id,
                    parent_id=child.parent_id,
                    chunk_type="child",
                    content=child.text,
                    section_path=child.section_path,
                    document_version=child.document_version,
                )
            )
        for parent in self._parents_list:
            self._store.upsert_chunk_record(
                self._make_chunk_record(
                    chunk_id=parent.parent_id,
                    parent_id=None,
                    chunk_type="parent",
                    content=parent.text,
                    section_path=parent.section_path,
                    document_version=parent.document_version,
                )
            )

    def _step_commit(self) -> None:
        expected_rev = self._store.get_current_revision(
            self._req.tenant_id, self._req.corpus_id, self._req.document_id
        )
        # Set indexed_at on the new row now (commit activates it).
        self._store.commit_active_version(
            tenant_id=self._req.tenant_id,
            corpus_id=self._req.corpus_id,
            document_id=self._req.document_id,
            new_version=self._req.document_version,
            expected_revision=expected_rev,
        )

    def _step_publish(self) -> None:
        self._ensure_chunked()
        acl = self._req.acl
        # New version becomes visible: flip its Qdrant points to active.
        new_points = [
            child_chunk_to_point(
                child,
                acl,
                status="active",
                deprecated=False,
                dense_encoder=self._dense,
                sparse_encoder=self._sparse,
            )
            for child in self._children_list
        ]
        self._vector.upsert(self._req.corpus_id, new_points)

        # Prior active versions (now deprecated in the Metadata DB) must be made
        # invisible on the data plane too. Re-derived from the control plane so a
        # resumed publish after a crash still finds them (in-memory state is lost
        # on restart; build plan §10.10 #3, #6).
        old_versions = self._store.get_superseded_versions(
            self._req.tenant_id,
            self._req.corpus_id,
            self._req.document_id,
            exclude_version=self._req.document_version,
        )
        for old_version in old_versions:
            if old_version == self._req.document_version:
                continue
            old_chunks = self._store.list_chunk_records(
                self._req.tenant_id,
                self._req.corpus_id,
                self._req.document_id,
                old_version,
            )
            old_points = []
            for rec in old_chunks:
                if rec.chunk_type == "parent":
                    self._parents.deprecate(rec.chunk_id)
                    continue
                child = ChildChunk(
                    child_id=rec.chunk_id,
                    parent_id=rec.parent_id or "",
                    document_id=rec.document_id,
                    document_version=rec.document_version,
                    tenant_id=rec.tenant_id,
                    corpus_id=rec.corpus_id,
                    text=rec.content,
                    section_path=rec.section_path,
                )
                old_points.append(
                    child_chunk_to_point(
                        child,
                        acl,
                        status="inactive",
                        deprecated=False,
                        dense_encoder=self._dense,
                        sparse_encoder=self._sparse,
                    )
                )
            if old_points:
                self._vector.upsert(self._req.corpus_id, old_points)

    def _step_finalize(self) -> None:
        self._store.mark_job_terminal(
            self._req.job_id,
            JobStatus.SUCCEEDED,
            parent_count=len(self._parents_list),
            child_count=len(self._children_list),
        )

    # ------------------------------------------------------------------ #
    # Compensation (build plan §10.5): on pre-commit failure, delete this
    # version's data-plane artifacts; never touch the existing active version.
    # ------------------------------------------------------------------ #
    def _compensate(self) -> None:
        point_ids = [child_point_id(c.child_id) for c in self._children_list]
        if point_ids:
            self._vector.delete(self._req.corpus_id, point_ids)
        for parent in self._parents_list:
            self._parents.delete(parent.parent_id)

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    def _auth_metadata(self) -> dict:
        acl = self._req.acl
        return {
            "status": "active",
            "deprecated": False,
            "security_level": acl.security_level,
            "acl_scope": acl.acl_scope,
            "allowed_user_ids": list(acl.allowed_user_ids),
            "allowed_group_ids": list(acl.allowed_group_ids),
            "denied_user_ids": list(acl.denied_user_ids),
            "denied_group_ids": list(acl.denied_group_ids),
        }

    def _build_source_document(self, *, status: DocumentStatus) -> SourceDocument:
        acl = self._req.acl
        now = _now()
        return SourceDocument(
            document_id=self._req.document_id,
            tenant_id=self._req.tenant_id,
            corpus_id=self._req.corpus_id,
            source_uri=self._req.source_uri or f"inline://{self._req.document_id}",
            source_connector=self._req.source_connector,
            source_native_id=self._req.source_native_id,
            title=self._req.title or self._req.document_id,
            source_filename=self._req.source_filename,
            mime_type=self._req.mime_type,
            version=self._req.document_version,
            content_hash=self._raw_hash,
            status=status,
            authority_level=self._req.authority_level,
            deprecated=False,
            acl_policy_id=self._req.acl_policy_id,
            security_level=acl.security_level,
            acl_scope=acl.acl_scope,
            allowed_user_ids=list(acl.allowed_user_ids),
            allowed_group_ids=list(acl.allowed_group_ids),
            denied_user_ids=list(acl.denied_user_ids),
            denied_group_ids=list(acl.denied_group_ids),
            parser_name=self._req.parser_name,
            parser_version=self._req.parser_version,
            chunking_version=self._req.chunking_version,
            embedding_model=self._req.embedding_model,
            embedding_version=self._req.embedding_version,
            discovered_at=now,
            last_synced_at=now,
        )

    def _make_chunk_record(
        self,
        *,
        chunk_id: str,
        parent_id: Optional[str],
        chunk_type: str,
        content: str,
        section_path: list[str],
        document_version: str,
    ) -> ChunkRecord:
        acl = self._req.acl
        return ChunkRecord(
            chunk_id=chunk_id,
            tenant_id=self._req.tenant_id,
            corpus_id=self._req.corpus_id,
            document_id=self._req.document_id,
            document_version=document_version,
            parent_id=parent_id,
            chunk_type=chunk_type,  # type: ignore[arg-type]
            section_path=section_path,
            content=content,
            content_hash=_sha256(content),
            authority_level=self._req.authority_level,
            deprecated=False,
            acl_policy_id=self._req.acl_policy_id,
            security_level=acl.security_level,
            acl_scope=acl.acl_scope,
            allowed_user_ids=list(acl.allowed_user_ids),
            allowed_group_ids=list(acl.allowed_group_ids),
            denied_user_ids=list(acl.denied_user_ids),
            denied_group_ids=list(acl.denied_group_ids),
            metadata={},
        )


class DocumentManager:
    """Thin facade over :class:`IngestionJob` (build plan §10.1 DocumentManager)."""

    def __init__(
        self,
        *,
        metadata_store: MetadataStore,
        vector_store: VectorStore,
        parent_store: ParentStore,
        chunker: ParentChildChunker,
        dense_encoder: DenseEncoder,
        sparse_encoder: SparseEncoder,
    ) -> None:
        self._store = metadata_store
        self._vector = vector_store
        self._parents = parent_store
        self._chunker = chunker
        self._dense = dense_encoder
        self._sparse = sparse_encoder

    def ingest(
        self, request: IngestionRequest, *, max_step: Optional[str] = None
    ) -> IngestionResult:
        job = IngestionJob(
            store=self._store,
            vector_store=self._vector,
            parent_store=self._parents,
            chunker=self._chunker,
            dense_encoder=self._dense,
            sparse_encoder=self._sparse,
            request=request,
        )
        return job.run(max_step=max_step)
