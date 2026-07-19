"""Persistent run checkpoint for the iteration loop (E-023 contract).

A :class:`RunCheckpoint` captures the resumable state of a
``ChatService.answer_with_iteration`` loop so a mid-loop crash/restart can be
continued by ``ChatService.resume_run``. The checkpoint is persisted as JSON in
the Metadata DB's ``run_checkpoints`` table (the control-plane source of truth).

The re-authorization helper :func:`reauthorize_evidence` is the security-critical
core: on resume it re-checks each gathered :class:`Evidence` against CURRENT
metadata (active document, active version, current ACL, corpus discoverability) and
drops anything the principal can no longer read (build plan §3623 — "ACL 收紧不因旧
Cache/Checkpoint 泄露").
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from agentic_rag_enterprise.domain.evidence import Evidence as SnapshotEvidence
from agentic_rag_enterprise.evidence.models import ConflictReport
from agentic_rag_enterprise.judge.models import RequiredFact, SufficiencyResult
from agentic_rag_enterprise.retrieval.fast_path import FastPathResult
from agentic_rag_enterprise.security.policy import (
    AuthorizationDecision,
    ResourceAcl,
    can_discover_corpus,
    evaluate_access,
)

if TYPE_CHECKING:
    from agentic_rag_enterprise.corpus.registry import CorpusRegistry
    from agentic_rag_enterprise.domain.security import SecurityContext
    from agentic_rag_enterprise.storage.metadata_store import MetadataStore

# Checkpoint lifecycle states.
CHECKPOINT_RUNNING = "running"
CHECKPOINT_COMPLETED = "completed"
CHECKPOINT_ABORTED = "aborted"


class RunCheckpoint(BaseModel):
    """Resumable state of one ``answer_with_iteration`` loop.

    Frozen so a stored checkpoint is never mutated in place; resume rebuilds a
    working copy. All nested models are Pydantic and JSON-serializable, so the
    whole object round-trips through ``model_dump_json`` / ``model_validate_json``.
    """

    model_config = ConfigDict(frozen=True)

    run_id: str
    tenant_id: str
    user_id: str
    session_id: str
    policy_version: str

    query: str
    corpus_id: str
    max_rounds: int
    required_facts: list[RequiredFact] = []

    # Next round to execute on resume (== rounds already completed).
    round_index: int = 0

    # Persistence lifecycle status (mirrors the ``run_checkpoints.status`` column
    # in the Metadata DB). Carried on the model so ``resume_run`` can refuse an
    # ``aborted`` checkpoint and idempotently return a ``completed`` one.
    status: str = CHECKPOINT_RUNNING

    # Accumulated, re-authorizable evidence gathered so far.
    evidence: tuple[SnapshotEvidence, ...] = ()

    # Loop trackers (rebuilt into dicts/sets on resume).
    prior_queries: list[str] = []
    seen_text_hashes: list[str] = []
    seen_doc_versions: list[tuple[str, str]] = []

    retrieval_calls: int = 0
    gap_rounds: int = 0
    final_reason: str | None = None
    conflict_stop: bool = False

    coverage: SufficiencyResult | None = None
    final_report: ConflictReport | None = None
    # Evidence ids that survived the last conflict stage; used to re-seed
    # ``final_evidence`` when resuming an already-completed run (no more rounds).
    final_evidence_ids: list[str] = []
    # Round-0 Fast Path result (frozen); needed to synthesize / refuse on resume
    # without re-issuing the initial retrieval.
    first_result: FastPathResult | None = None

    def to_json(self) -> str:
        return self.model_dump_json()

    @classmethod
    def from_json(cls, data: str) -> "RunCheckpoint":
        return cls.model_validate_json(data)


class ResumeAuthError(Exception):
    """Raised when a checkpoint cannot be resumed (fail-closed).

    The message is safe to log internally but MUST NOT leak tenant/evidence ids to
    the client (build plan §5.4 / §12.8). The API maps it to a generic 5xx.
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class CheckpointIdentityConflict(Exception):
    """Raised when a ``run_id`` is reused with a different immutable binding.

    The ``run_id`` is a client-supplied identifier, but the checkpoint it names is
    bound to an immutable identity (tenant / user / session / query / corpus /
    policy_version). A second writer attempting to reuse an existing ``run_id``
    under a *different* identity must be refused so it cannot hijack or overwrite
    another principal's checkpoint (build plan §5.4). The original row is left
    untouched. The message carries only the ``run_id`` (never tenant / evidence
    identifiers) and is mapped to a generic 4xx by the API.
    """

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        super().__init__(f"checkpoint run_id already bound to a different identity: {run_id}")


def reauthorize_evidence(
    evidence: SnapshotEvidence,
    ctx: "SecurityContext",
    *,
    metadata_store: "MetadataStore",
    registry: "CorpusRegistry",
) -> tuple[bool, str]:
    """Re-check one gathered ``Evidence`` against CURRENT authorization state.

    Returns ``(kept, reason)``. The evidence is kept only if ALL hold (fail
    closed — any failure drops it):

    * the corpus is still discoverable for ``ctx`` (``registry.get`` succeeds);
    * the document has a current **active**, non-deprecated row;
    * the evidence's ``document_version`` is still the active version (a
      re-versioned document's old points are no longer the visible truth);
    * the current document ACL allows ``ctx`` via the canonical PDP.

    This mirrors ``storage/evidence_store.py``'s read-time re-authorization and
    guarantees an old checkpoint cannot surface data the principal lost access to.
    """
    # Corpus discoverability (fail closed on any error).
    try:
        registry.get(evidence.corpus_id, ctx)
    except Exception:  # noqa: BLE001 - any discoverability failure drops the evidence
        return False, "corpus_not_discoverable"

    active = metadata_store.get_active_document(
        evidence.tenant_id, evidence.corpus_id, evidence.document_id
    )
    if active is None:
        return False, "document_inactive_or_deleted"
    if evidence.document_version != active.version:
        return False, "version_superseded"

    acl = ResourceAcl(
        tenant_id=active.tenant_id,
        security_level=active.security_level,
        acl_scope=active.acl_scope,
        allowed_user_ids=active.allowed_user_ids,
        allowed_group_ids=active.allowed_group_ids,
        denied_user_ids=active.denied_user_ids,
        denied_group_ids=active.denied_group_ids,
    )
    if evaluate_access(ctx, acl) is not AuthorizationDecision.ALLOW:
        return False, "acl_denied"
    if not can_discover_corpus(ctx, evidence.corpus_id):
        return False, "corpus_not_discoverable"
    return True, "authorized"
