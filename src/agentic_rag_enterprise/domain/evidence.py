"""Evidence snapshot model — immutable at answer time."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Evidence(BaseModel):
    model_config = ConfigDict(frozen=True)

    evidence_id: str

    tenant_id: str
    corpus_id: str

    document_id: str
    document_version: str
    source_uri: str
    source_filename: str

    parent_id: str | None = None
    child_chunk_id: str | None = None

    page_number: int | None = None
    section_path: tuple[str, ...] = Field(default_factory=tuple)
    start_offset: int | None = None
    end_offset: int | None = None

    text: str
    text_hash: str

    retrieval_query: str
    retrieval_score: float | None = None
    rerank_score: float | None = None

    authority_level: int
    effective_from: datetime | None = None
    effective_to: datetime | None = None
    deprecated: bool = False

    retrieved_at: datetime
    acl_policy_id: str
    policy_version: str

    retrieval_iteration: int
    plan_step_id: str | None = None

    @field_validator("authority_level")
    @classmethod
    def _authority_bounds(cls, v: int) -> int:
        if v < 0 or v > 100:
            raise ValueError("authority_level must be between 0 and 100")
        return v
