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
from agentic_rag_enterprise.domain.ingestion import (
    DocumentStatus,
    IngestionManifest,
    JobStatus,
)
from agentic_rag_enterprise.ingestion.chunker import ChildChunk, ParentChildChunker, ParentChunk
from agentic_rag_enterprise.security.policy import ResourceAcl
from agentic_rag_enterprise.storage.metadata_store import (
    ActiveVersionConflict,
    JobIdentityConflict,
    MetadataStore,
    VersionContentConflict,
)
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
    IN_PROGRESS = "in_progress"
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
        "verify",
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
        stopped_early = False
        if max_step is not None:
            if max_step not in steps:
                raise ValueError(f"unknown step: {max_step}")
            if max_step != steps[-1]:
                stopped_early = True
            steps = steps[: steps.index(max_step) + 1]

        # Hash the content up front; it drives identity and idempotency.
        self._raw_hash = _sha256(self._req.content)

        # P1-4 (fast pre-check): a job_id is an immutable binding. Reject reuse
        # with a different request up front; the authoritative atomic check lives
        # in acquire_job (single transaction) and guards concurrent deliveries.
        self._store.validate_job_identity(
            job_id=self._req.job_id,
            tenant_id=self._req.tenant_id,
            corpus_id=self._req.corpus_id,
            document_id=self._req.document_id,
            document_version=self._req.document_version,
            raw_hash=self._raw_hash,
        )

        # P1-2: idempotency keyed on (document, version, content_hash), NOT on
        # job_id. Same artifact already known -> ALREADY_INDEXED (no rework,
        # no overwrite of the active row, no data-plane rewrite). Same version
        # with different content -> VersionContentConflict (never overwrite).
        # A job_id is an immutable binding; that check is folded into the atomic
        # acquire_job (E-008.2 P1-4, no TOCTOU).
        existing = self._store.get_document(
            self._req.tenant_id,
            self._req.corpus_id,
            self._req.document_id,
            self._req.document_version,
        )
        if existing is not None:
            if existing.content_hash != self._raw_hash:
                raise VersionContentConflict(
                    f"version {self._req.document_version!r} already ingested with a "
                    f"different content_hash; refusing to overwrite"
                )
            if existing.status == DocumentStatus.ACTIVE:
                # P1-1: a published version is only ALREADY_INDEXED when the build
                # that produced it has fully completed (owner job SUCCEEDED). If
                # the owning build is still in-flight or crashed between commit
                # and publish, we must RESUME and finish publish/finalize rather
                # than short-circuit (otherwise a committed-but-unpublished active
                # version would never reach the data plane).
                owner = self._store.get_build_owner(
                    self._req.tenant_id,
                    self._req.corpus_id,
                    self._req.document_id,
                    self._req.document_version,
                )
                owner_status = self._store.get_job_status(owner) if owner else None
                if owner_status == JobStatus.SUCCEEDED:
                    # Fully published. Still guard the job_id immutable binding
                    # (a reused job_id with a different request must fail closed).
                    identity = self._store.get_job_identity(self._req.job_id)
                    if identity is not None and (
                        identity["tenant_id"] != self._req.tenant_id
                        or identity["corpus_id"] != self._req.corpus_id
                        or identity["document_id"] != self._req.document_id
                        or identity["document_version"] != self._req.document_version
                        or (identity["raw_hash"] or "") != self._raw_hash
                    ):
                        raise JobIdentityConflict(
                            f"job_id={self._req.job_id!r} already bound to a "
                            f"different request"
                        )
                    # P2-1: only mark terminal if this job actually owns a row.
                    if self._store.get_job_status(self._req.job_id) is not None:
                        self._store.mark_job_terminal(
                            self._req.job_id, JobStatus.SUCCEEDED
                        )
                    return IngestionResult(
                        status=IngestionStatus.ALREADY_INDEXED,
                        job_id=self._req.job_id,
                        document_version=self._req.document_version,
                    )
            # processing/active-but-not-fully-published same-content re-delivery:
            # fall through and run (resume) rather than clobbering active state.

        try:
            for step in steps:
                if self._store.is_step_done(self._req.job_id, step):
                    continue
                getattr(self, f"_step_{step}")()
                self._store.mark_step(self._req.job_id, step, "done")

            if stopped_early:
                return IngestionResult(
                    status=IngestionStatus.IN_PROGRESS,
                    job_id=self._req.job_id,
                    document_version=self._req.document_version,
                )
            return IngestionResult(
                status=IngestionStatus.INDEXED,
                job_id=self._req.job_id,
                document_version=self._req.document_version,
                parent_count=len(self._parents_list),
                child_count=len(self._children_list),
            )
        except Exception as exc:  # noqa: BLE001 — fail closed + compensate
            # P1-5: every pre-commit failure path (including ActiveVersionConflict)
            # routes through the same idempotent compensation and leaves a failed
            # record. Never deactivates the currently visible active version.
            error_code = (
                "active_version_conflict"
                if isinstance(exc, ActiveVersionConflict)
                else "ingestion_error"
            )
            if not self._store.is_step_done(self._req.job_id, "commit"):
                self._compensate()
            else:
                # Committed but a later step (publish/finalize) failed: the new
                # version is already visible in the control plane, so do NOT roll
                # it back (build plan §10.10 #6). Crucially we must PRESERVE
                # previous_active_version: recovery's publish step still needs it
                # to deprecate the version this commit replaced on the data plane.
                # Clearing it (as before) left the old Qdrant points / parents
                # orphaned and un-cleaned after recovery (E-008.2 P1-2).
                pass
            self._store.mark_job_terminal(
                self._req.job_id,
                JobStatus.FAILED,
                error_code=error_code,
                error_message=str(exc),
            )
            return IngestionResult(
                status=IngestionStatus.FAILED,
                job_id=self._req.job_id,
                document_version=self._req.document_version,
                error_code=error_code,
                error_message=str(exc),
            )

    # ------------------------------------------------------------------ #
    # Steps
    # ------------------------------------------------------------------ #
    def _step_acquire(self) -> None:
        # Create the document control-plane row FIRST so the ingestion_jobs FK
        # (document_id, tenant_id, corpus_id, document_version) is satisfied.
        self._source_doc = self._build_source_document(status=DocumentStatus.PROCESSING)
        self._store.upsert_document(self._source_doc)
        # P1-3: capture and persist the monotonic lifecycle revision at acquire
        # time. The commit phase CASes against THIS value, so a newer revision
        # landing first makes this (older) job lose the race.
        base_revision = self._store.get_current_revision(
            self._req.tenant_id, self._req.corpus_id, self._req.document_id
        )
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
            base_revision=base_revision,
        )
        # P1-7: capture the version this job is intended to replace. This is
        # sampled at acquire time and persisted, so a resumed job (after commit
        # but before publish) still deprecates the ORIGINAL previous version and
        # never the version it already committed. acquire_job is an
        # INSERT OR REPLACE, so restore any previously captured value.
        existing_previous = self._store.get_job_previous_version(self._req.job_id)
        if existing_previous is None:
            previous = self._store.get_active_version(
                self._req.tenant_id, self._req.corpus_id, self._req.document_id
            )
        else:
            previous = existing_previous
        self._store.set_job_previous_version(self._req.job_id, previous)

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
        # P1-3: commit against the revision captured at acquire time, not the
        # latest value read just before commit (which would let an older job
        # overwrite a newer committed state).
        expected_rev = self._store.get_job_base_revision(self._req.job_id)
        new_rev, previous_version = self._store.commit_active_version(
            tenant_id=self._req.tenant_id,
            corpus_id=self._req.corpus_id,
            document_id=self._req.document_id,
            new_version=self._req.document_version,
            expected_revision=expected_rev,
        )
        # The version this job replaces is captured at acquire time (stable
        # across resume); commit_active_version's returned previous is only the
        # current active version and may be the job's own version on resume, so
        # it must NOT overwrite the persisted acquire-time value.

    def _step_verify(self) -> None:
        """Verify data-plane completeness before the active-version switch.

        Build plan §10.10 #4: the data plane must be fully written AND verified
        before commit. Confirms expected Parent IDs and Qdrant Point IDs exist,
        identity (tenant/corpus/document/version/parent/chunk) is truly consistent
        (not just column presence), counts match the chunker output, and the new
        version is still committable.
        """
        self._ensure_chunked()
        collection = self._req.corpus_id

        # All expected parents present in the Parent Store WITH consistent
        # identity (tenant/corpus/document/version/parent_id).
        for parent in self._parents_list:
            stored = self._parents.get(parent.parent_id)
            if stored is None:
                raise RuntimeError(
                    f"verify failed: parent {parent.parent_id} missing from Parent Store"
                )
            if (
                stored.tenant_id != self._req.tenant_id
                or stored.corpus_id != self._req.corpus_id
                or stored.document_id != self._req.document_id
                or stored.document_version != self._req.document_version
                or stored.parent_id != parent.parent_id
            ):
                raise RuntimeError(
                    f"verify failed: identity mismatch on parent {parent.parent_id}"
                )

        # All expected Qdrant points exist WITH consistent identity (read the
        # payload back, not just column presence).
        point_ids = [child_point_id(c.child_id) for c in self._children_list]
        if point_ids:
            found = self._vector._client.retrieve(
                collection_name=collection, ids=point_ids, with_payload=True
            )
            found_by_id = {str(p.id): p for p in found}
            missing = [pid for pid in point_ids if pid not in found_by_id]
            if missing:
                raise RuntimeError(f"verify failed: {len(missing)} Qdrant point(s) missing")
            for pid in point_ids:
                p = found_by_id[pid]
                payload = p.payload or {}
                if (
                    payload.get("tenant_id") != self._req.tenant_id
                    or payload.get("corpus_id") != self._req.corpus_id
                    or payload.get("document_id") != self._req.document_id
                    or payload.get("document_version") != self._req.document_version
                    or not payload.get("parent_id")
                    or not payload.get("chunk_id")
                ):
                    raise RuntimeError(
                        f"verify failed: identity mismatch on Qdrant point {pid}"
                    )

        # Identity + count consistency against persisted chunk records.
        records = self._store.list_chunk_records(
            self._req.tenant_id,
            self._req.corpus_id,
            self._req.document_id,
            self._req.document_version,
        )
        if len(records) != len(self._parents_list) + len(self._children_list):
            raise RuntimeError(
                f"verify failed: chunk record count {len(records)} != "
                f"{len(self._parents_list) + len(self._children_list)}"
            )
        for rec in records:
            if (
                rec.tenant_id != self._req.tenant_id
                or rec.corpus_id != self._req.corpus_id
                or rec.document_id != self._req.document_id
                or rec.document_version != self._req.document_version
            ):
                raise RuntimeError(f"verify failed: identity mismatch on chunk {rec.chunk_id}")

        # New version must still be committable (processing / failed / active-on-
        # resume). The CAS in commit_active_version is the authoritative race
        # guard; this just rejects impossible statuses.
        current = self._store.get_document(
            self._req.tenant_id,
            self._req.corpus_id,
            self._req.document_id,
            self._req.document_version,
        )
        if current is None:
            raise RuntimeError("verify failed: new version missing before commit")
        if current.status not in (
            DocumentStatus.PROCESSING,
            DocumentStatus.FAILED,
            DocumentStatus.ACTIVE,
        ):
            raise RuntimeError(f"verify failed: unexpected status {current.status}")

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

        # Promote THIS version's parents from "processing" to "active" so the
        # second-auth pass in ParentReader admits them. Only the parents THIS job
        # produced (scoped by the chunker output, not a corpus-wide version scan)
        # are touched, so a concurrent job's parents are never disturbed (P2-2).
        for parent in self._parents_list:
            chunk = self._parents.get(parent.parent_id)
            if chunk is None:
                continue
            md = dict(chunk.metadata)
            md["status"] = "active"
            md["deprecated"] = False
            self._parents.put(chunk.model_copy(update={"metadata": md}))

        # P1-7: deprecate ONLY the version this commit actually replaced (read
        # from the persisted job record, not a scan of all non-active rows), so
        # a concurrent job's still-processing version is never disturbed.
        previous_version = self._store.get_job_previous_version(self._req.job_id)
        if not previous_version or previous_version == self._req.document_version:
            return
        old_chunks = self._store.list_chunk_records(
            self._req.tenant_id,
            self._req.corpus_id,
            self._req.document_id,
            previous_version,
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
        # Build and persist the ingestion manifest (build plan §10.9).
        manifest = IngestionManifest(
            job_id=self._req.job_id,
            document_id=self._req.document_id,
            document_version=self._req.document_version,
            corpus_id=self._req.corpus_id,
            tenant_id=self._req.tenant_id,
            status=JobStatus.SUCCEEDED,
            started_at=_now(),
            finished_at=_now(),
            raw_hash=self._raw_hash,
            parsed_hash=self._parsed_hash or None,
            parent_count=len(self._parents_list),
            child_count=len(self._children_list),
            parser_version=self._req.parser_version,
            chunking_version=self._req.chunking_version,
            embedding_version=self._req.embedding_version,
        )
        self._store.set_job_manifest(self._req.job_id, manifest.model_dump_json())
        self._store.mark_job_terminal(
            self._req.job_id,
            JobStatus.SUCCEEDED,
            parent_count=len(self._parents_list),
            child_count=len(self._children_list),
        )

    # ------------------------------------------------------------------ #
    # Compensation (build plan §10.5 / §10.10 #7): on pre-commit failure,
    # delete THIS version's data-plane artifacts; never touch the existing
    # active version. Idempotent and does NOT rely on in-memory state: it
    # re-derives chunk ids deterministically and also removes the control-plane
    # chunk records and marks the processing document row failed.
    # ------------------------------------------------------------------ #
    def _compensate(self) -> None:
        self._ensure_chunked()
        point_ids = [child_point_id(c.child_id) for c in self._children_list]
        if point_ids:
            self._vector.delete(self._req.corpus_id, point_ids)
        for parent in self._parents_list:
            self._parents.delete(parent.parent_id)
        self._store.delete_chunk_records(
            self._req.tenant_id,
            self._req.corpus_id,
            self._req.document_id,
            self._req.document_version,
        )
        self._store.clear_steps(self._req.job_id)
        self._store.mark_document_failed(
            self._req.tenant_id,
            self._req.corpus_id,
            self._req.document_id,
            self._req.document_version,
        )

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
