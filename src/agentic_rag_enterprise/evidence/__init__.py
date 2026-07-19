"""E-021 evidence-stage conflict handling (post-retrieval, pre-sufficiency)."""

from agentic_rag_enterprise.evidence.conflict_resolver import (
    AUTHORITY_OVERRIDE_MARGIN,
    ConflictResolver,
)
from agentic_rag_enterprise.evidence.models import (
    AssertionExtraction,
    ConflictFinding,
    ConflictReport,
    ConflictResolution,
    ConflictStatus,
    ConflictType,
    SourceRef,
    extract_assertion,
    normalize_topic_key,
)
from agentic_rag_enterprise.evidence.temporal import (
    FilteredEvidence,
    TemporalFilterResult,
    filter_by_temporal_scope,
)

__all__ = [
    "ConflictResolver",
    "AUTHORITY_OVERRIDE_MARGIN",
    "ConflictReport",
    "ConflictFinding",
    "ConflictType",
    "ConflictStatus",
    "ConflictResolution",
    "SourceRef",
    "AssertionExtraction",
    "extract_assertion",
    "normalize_topic_key",
    "filter_by_temporal_scope",
    "TemporalFilterResult",
    "FilteredEvidence",
]
