"""Typed retrieval result models for the E-007 secure retrieval path."""

from typing import Literal

from pydantic import BaseModel, Field

AclScope = Literal["tenant", "restricted"]


def as_acl_scope(value: object) -> AclScope:
    """Coerce an arbitrary payload/metadata value to the ACL-scope literal.

    Only ``"tenant"`` is treated as tenant-scoped; everything else (including
    missing/unknown values) falls back to the stricter ``"restricted"``.
    """
    return "tenant" if value == "tenant" else "restricted"


class RetrievalHit(BaseModel):
    """A single authorized child-chunk hit returned by hybrid retrieval.

    Carries the document ACL fields (mirrored from the Qdrant payload) so the
    parent reader can verify the stored parent's ACL has not diverged from the
    child's at read time.
    """

    chunk_id: str
    parent_id: str
    document_id: str
    document_version: str
    corpus_id: str
    tenant_id: str
    text: str
    score: float
    section_path: list[str] = Field(default_factory=list)

    status: str = "active"
    deprecated: bool = False
    security_level: str
    acl_scope: Literal["tenant", "restricted"] = "restricted"
    allowed_user_ids: list[str] = Field(default_factory=list)
    allowed_group_ids: list[str] = Field(default_factory=list)
    denied_user_ids: list[str] = Field(default_factory=list)
    denied_group_ids: list[str] = Field(default_factory=list)


class AuthorizedParent(BaseModel):
    """A parent chunk that passed the second authorization pass."""

    parent_id: str
    document_id: str
    document_version: str
    corpus_id: str
    tenant_id: str
    content: str
    section_path: list[str] = Field(default_factory=list)
    metadata: dict[str, object] = Field(default_factory=dict)

    status: str = "active"
    deprecated: bool = False
    security_level: str
    acl_scope: Literal["tenant", "restricted"] = "restricted"
    allowed_user_ids: list[str] = Field(default_factory=list)
    allowed_group_ids: list[str] = Field(default_factory=list)
    denied_user_ids: list[str] = Field(default_factory=list)
    denied_group_ids: list[str] = Field(default_factory=list)


class RetrievalResult(BaseModel):
    """Typed output of the secure retrieval path."""

    hits: list[tuple[RetrievalHit, AuthorizedParent]] = Field(default_factory=list)
    denied_parent_count: int = 0
    # §12.9 telemetry only. ``exclude=True`` keeps it out of any serialized
    # output so the end user never learns whether/why an unauthorized resource
    # exists. Read the attribute directly for internal observability.
    denied_reasons: dict[str, int] = Field(default_factory=dict, exclude=True)


class ParentAuthorizationError(Exception):
    """Raised when a parent fails the second authorization pass (fail closed).

    Carries a build plan §12.9 ``code`` so callers can classify the denial for
    telemetry WITHOUT leaking to the end user whether an unauthorized resource
    exists. Base code is ``PARENT_NOT_AUTHORIZED``; subclasses refine it.
    """

    code = "PARENT_NOT_AUTHORIZED"

    def __init__(self, reason: str, parent_id: str = "") -> None:
        self.reason = reason
        self.parent_id = parent_id
        super().__init__(f"parent authorization failed for {parent_id or '<unknown>'}: {reason}")


class ParentNotFoundError(ParentAuthorizationError):
    """§12.9 ``PARENT_NOT_FOUND`` — parent id absent from the store."""

    code = "PARENT_NOT_FOUND"


class ParentNotAuthorizedError(ParentAuthorizationError):
    """§12.9 ``PARENT_NOT_AUTHORIZED`` — identity/ACL/visibility denial."""

    code = "PARENT_NOT_AUTHORIZED"


class ParentDeletedError(ParentAuthorizationError):
    """§12.9 ``DOCUMENT_DELETED`` — parent lifecycle not active / deprecated."""

    code = "DOCUMENT_DELETED"


class ParentVersionMismatchError(ParentAuthorizationError):
    """§12.9 ``VERSION_MISMATCH`` — parent version diverges from the hit."""

    code = "VERSION_MISMATCH"


class CorpusNotDiscoverableError(Exception):
    """Raised when a corpus fails the discoverability/tenant/enabled gate."""
