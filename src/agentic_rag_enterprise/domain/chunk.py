"""Chunk domain model with parent-child hierarchy and provenance metadata."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


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

    @field_validator("authority_level")
    @classmethod
    def _authority_bounds(cls, v: int) -> int:
        if v < 0 or v > 100:
            raise ValueError("authority_level must be between 0 and 100")
        return v

    @field_validator("start_offset")
    @classmethod
    def _offset_non_negative(cls, v: int | None) -> int | None:
        if v is not None and v < 0:
            raise ValueError("start_offset must be non-negative")
        return v

    @field_validator("end_offset")
    @classmethod
    def _end_offset_valid(cls, v: int | None, info) -> int | None:
        if v is not None and v < 0:
            raise ValueError("end_offset must be non-negative")
        start = info.data.get("start_offset")
        if v is not None and start is not None and v < start:
            raise ValueError("end_offset must not be less than start_offset")
        return v

    @field_validator("effective_to")
    @classmethod
    def _effective_ordering(cls, v: datetime | None, info) -> datetime | None:
        if v is not None and info.data.get("effective_from") is not None:
            if v < info.data["effective_from"]:
                raise ValueError("effective_to must not be earlier than effective_from")
        return v

    @model_validator(mode="after")
    def _offset_consistency(self) -> "ChunkRecord":
        start_set = self.start_offset is not None
        end_set = self.end_offset is not None
        if start_set != end_set:
            raise ValueError("start_offset and end_offset must both be set or both be null")
        return self
