"""Chunk domain model with parent-child hierarchy and provenance metadata."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class ChunkRecord(BaseModel):
    chunk_id: str
    tenant_id: str
    corpus_id: str

    document_id: str
    document_version: str

    parent_id: str | None = None
    chunk_type: Literal["parent", "child"]

    page_number: int | None = None
    section_path: list[str] = Field(default_factory=list)

    start_offset: int | None = None
    end_offset: int | None = None

    content: str
    content_hash: str

    effective_from: datetime | None = None
    effective_to: datetime | None = None

    authority_level: int = 50
    deprecated: bool = False

    acl_policy_id: str
    security_level: str
    acl_scope: Literal["tenant", "restricted"] = "restricted"
    allowed_user_ids: list[str] = Field(default_factory=list)
    allowed_group_ids: list[str] = Field(default_factory=list)
    denied_user_ids: list[str] = Field(default_factory=list)
    denied_group_ids: list[str] = Field(default_factory=list)

    metadata: dict[str, object] = Field(default_factory=dict)
