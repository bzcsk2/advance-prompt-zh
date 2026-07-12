"""Security context model propagated through the request lifecycle."""

from pydantic import BaseModel, Field


class SecurityContext(BaseModel):
    request_id: str
    session_id: str

    tenant_id: str
    user_id: str

    roles: list[str] = Field(default_factory=list)
    groups: list[str] = Field(default_factory=list)

    allowed_security_levels: list[str] = Field(default_factory=lambda: ["public", "internal"])
    allowed_corpus_ids: list[str] | None = None

    policy_version: str

    is_admin: bool = False

    # Fine-grained runtime permissions (e.g. "audit:evidence:read"). Distinct
    # from `roles`/`groups`: these are capability grants the runtime injects,
    # never model-supplied. Used by the Evidence Store read-time re-authorization
    # (build plan §12.8) so an independent, auditable grant can access revoked
    # evidence bodies without `is_admin` implying blanket read access.
    permissions: list[str] = Field(default_factory=list)
