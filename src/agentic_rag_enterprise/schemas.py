from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class SufficiencyStatus(str, Enum):
    SUFFICIENT = "sufficient"
    INSUFFICIENT = "insufficient"
    AMBIGUOUS = "ambiguous"
    UNANSWERABLE = "unanswerable"


class CorpusConfig(BaseModel):
    corpus_id: str
    name: str
    description: str
    domain: str = "general"
    owner: str = "unknown"
    collection_name: str
    metadata_filters: dict[str, Any] = Field(default_factory=dict)
    access_policy: dict[str, Any] = Field(default_factory=dict)


class SubQuestion(BaseModel):
    id: str
    question: str
    target_corpora: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)


class QueryPlan(BaseModel):
    task_type: str = "single_hop"
    required_facts: list[str] = Field(default_factory=list)
    subquestions: list[SubQuestion] = Field(default_factory=list)


class Evidence(BaseModel):
    evidence_id: str
    corpus_id: str
    document_id: str
    chunk_id: str
    text: str
    score: float | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class SufficiencyDecision(BaseModel):
    status: SufficiencyStatus
    covered_facts: list[str] = Field(default_factory=list)
    missing_facts: list[str] = Field(default_factory=list)
    contradictions: list[str] = Field(default_factory=list)
    next_queries: list[str] = Field(default_factory=list)
    target_corpora: list[str] = Field(default_factory=list)
    reason: str = ""


class GroundedAnswer(BaseModel):
    answer: str
    citations: list[str] = Field(default_factory=list)
    confidence: str = "unknown"
    completeness_note: str = ""
    abstained: bool = False
