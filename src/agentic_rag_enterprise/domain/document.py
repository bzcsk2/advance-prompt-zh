"""Document domain model with versioning, ACL, and lifecycle state."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from agentic_rag_enterprise.domain.ingestion import DocumentStatus


class SourceDocument(BaseModel):
    document_id: str
    tenant_id: str
    corpus_id: str

    source_uri: str
    source_connector: str
    source_native_id: str | None = None

    title: str
    source_filename: str
    mime_type: str

    version: str
    content_hash: str

    status: DocumentStatus

    effective_from: datetime | None = None
    effective_to: datetime | None = None

    authority_level: int = 50
    deprecated: bool = False
    supersedes_document_id: str | None = None

    acl_policy_id: str
    security_level: str
    acl_scope: Literal["tenant", "restricted"] = "restricted"
    allowed_user_ids: list[str] = Field(default_factory=list)
    allowed_group_ids: list[str] = Field(default_factory=list)
    denied_user_ids: list[str] = Field(default_factory=list)
    denied_group_ids: list[str] = Field(default_factory=list)

    parser_name: str
    parser_version: str
    chunking_version: str
    embedding_model: str
    embedding_version: str

    discovered_at: datetime
    indexed_at: datetime | None = None
    deleted_at: datetime | None = None
    last_synced_at: datetime

    @field_validator("authority_level")
    @classmethod
    def _authority_bounds(cls, v: int) -> int:
        if v < 0 or v > 100:
            raise ValueError("authority_level must be between 0 and 100")
        return v

    @field_validator("effective_to")
    @classmethod
    def _effective_ordering(cls, v: datetime | None, info) -> datetime | None:
        if v is not None and info.data.get("effective_from") is not None:
            if v < info.data["effective_from"]:
                raise ValueError("effective_to must not be earlier than effective_from")
        return v

    def model_post_init(self, /, __context) -> None:
        if self.status == DocumentStatus.ACTIVE and self.indexed_at is None:
            raise ValueError("active document must have indexed_at set")
        if self.status == DocumentStatus.DELETED and self.deleted_at is None:
            raise ValueError("deleted document must have deleted_at set")
