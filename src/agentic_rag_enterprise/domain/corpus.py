"""Corpus configuration model."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class CorpusConfig(BaseModel):
    corpus_id: str
    tenant_id: str
    name: str
    description: str
    domain: str
    owner: str

    source_type: Literal[
        "documents",
        "tickets",
        "wiki",
        "database",
        "api",
        "graph",
    ]

    capability_ids: list[str]

    vector_collection: str | None = None
    parent_store_namespace: str | None = None

    enabled: bool = True
    searchable: bool = True

    authority_level: int = Field(default=50, ge=0, le=100)
    freshness_sla_hours: int | None = None

    security_policy_id: str
    default_security_level: str = "internal"

    metadata_schema: dict[str, str] = Field(default_factory=dict)

    created_at: datetime
    updated_at: datetime
