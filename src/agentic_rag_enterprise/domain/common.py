"""Common type aliases and base types shared across all domain models."""

from typing import NewType

TenantId = NewType("TenantId", str)
UserId = NewType("UserId", str)
CorpusId = NewType("CorpusId", str)
DocumentId = NewType("DocumentId", str)
DocumentVersion = NewType("DocumentVersion", str)
ChunkId = NewType("ChunkId", str)
EvidenceId = NewType("EvidenceId", str)
ClaimId = NewType("ClaimId", str)
PlanId = NewType("PlanId", str)
PlanStepId = NewType("PlanStepId", str)
RequestId = NewType("RequestId", str)
SessionId = NewType("SessionId", str)

__all__ = [
    "TenantId",
    "UserId",
    "CorpusId",
    "DocumentId",
    "DocumentVersion",
    "ChunkId",
    "EvidenceId",
    "ClaimId",
    "PlanId",
    "PlanStepId",
    "RequestId",
    "SessionId",
]
