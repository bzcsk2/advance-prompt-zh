"""Document domain model with versioning, ACL, and lifecycle state."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


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

    status: Literal[
        "discovered",
        "processing",
        "active",
        "failed",
        "deprecated",
        "deleted",
    ]

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
