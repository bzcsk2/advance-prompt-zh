"""Ingestion manifest and lifecycle state machine."""

from datetime import datetime
from enum import Enum

from pydantic import BaseModel


class DocumentStatus(str, Enum):
    DISCOVERED = "discovered"
    PROCESSING = "processing"
    ACTIVE = "active"
    FAILED = "failed"
    DEPRECATED = "deprecated"
    DELETED = "deleted"


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"


DOCUMENT_LIFECYCLE: dict[DocumentStatus, list[DocumentStatus]] = {
    DocumentStatus.DISCOVERED: [DocumentStatus.PROCESSING],
    DocumentStatus.PROCESSING: [
        DocumentStatus.ACTIVE,
        DocumentStatus.FAILED,
    ],
    DocumentStatus.ACTIVE: [
        DocumentStatus.PROCESSING,
        DocumentStatus.DEPRECATED,
        DocumentStatus.DELETED,
    ],
    DocumentStatus.FAILED: [
        DocumentStatus.PROCESSING,
        DocumentStatus.DELETED,
    ],
    DocumentStatus.DEPRECATED: [DocumentStatus.DELETED],
    DocumentStatus.DELETED: [],
}


def valid_transition(from_status: DocumentStatus, to_status: DocumentStatus) -> bool:
    return to_status in DOCUMENT_LIFECYCLE.get(from_status, [])


class IngestionManifest(BaseModel):
    job_id: str
    document_id: str
    document_version: str
    corpus_id: str

    status: str
    started_at: datetime
    finished_at: datetime | None = None

    raw_hash: str
    parsed_hash: str | None = None

    parent_count: int = 0
    child_count: int = 0

    parser_version: str
    chunking_version: str
    embedding_version: str

    error_code: str | None = None
    error_message: str | None = None
