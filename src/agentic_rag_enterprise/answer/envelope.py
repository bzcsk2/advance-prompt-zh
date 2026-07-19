"""E-013 typed answer models (build plan §7.9 / §16).

``AnswerEnvelope`` wraps a caller-supplied grounded answer plus its claims and
the Evidence Snapshots those claims cite. The model is deeply frozen and
validated so its fields can never contradict one another (mirrors the E-012
validated-model approach):

* ``Claim`` and ``Citation`` are themselves frozen (no post-construction
  mutation of ``evidence_ids`` or ``document_version``);
* every ``Claim.evidence_ids`` entry and every ``Citation.evidence_id`` resolves
  to an ``evidence`` snapshot (no dangling citation);
* ``abstained is True`` ⇒ empty claims/evidence + ``completeness == insufficient``
  + ``stop_reason == no_evidence``;
* ``completeness == insufficient`` ⇒ ``abstained is True``.

These are the *real* enterprise models — deliberately distinct from the M0
baseline mocks in ``schemas.py`` (``GroundedAnswer`` / ``Evidence``).
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agentic_rag_enterprise.domain.evidence import Evidence as SnapshotEvidence
from agentic_rag_enterprise.evidence.models import ConflictReport, ConflictStatus
from agentic_rag_enterprise.judge.models import SufficiencyResult

ClaimImportance = Literal["critical", "supporting", "minor"]
ClaimSupport = Literal["entailed", "partially_entailed", "contradicted", "unsupported"]
Completeness = Literal["complete", "partial", "insufficient", "conflicted"]
Confidence = Literal["high", "medium", "low"]


class AnswerEnvelopeError(Exception):
    """Base error for AnswerEnvelope construction / binding failures."""


class TenantBindingError(AnswerEnvelopeError):
    """Raised when the SecurityContext and FastPathResult/Evidence tenants mismatch.

    Cross-tenant leakage is fail-closed: a tenant-b Context must never wrap a
    tenant-a Evidence Snapshot (build plan §12.8 / M2 single-tenant invariant).
    """


class Claim(BaseModel):
    """An atomic claim extracted from the answer (build plan §16.3).

    Frozen: ``evidence_ids`` is a tuple so a constructed claim cannot later be
    mutated to cite a dangling Evidence id.
    """

    model_config = ConfigDict(frozen=True)

    claim_id: str
    text: str
    importance: ClaimImportance = "supporting"
    evidence_ids: tuple[str, ...] = Field(default_factory=tuple)
    support_status: ClaimSupport = "entailed"


class Citation(BaseModel):
    """An immutable, resolvable citation to an Evidence Snapshot (§16.5 / §16.6).

    Frozen and carries the 1-based UI index plus the snapshot's source
    coordinates and the immutable reference fields so an audit record can replay
    the exact snapshot the claim was grounded on — never a live link to the
    latest source document.
    """

    model_config = ConfigDict(frozen=True)

    index: int
    evidence_id: str

    corpus_id: str
    document_id: str
    document_version: str
    section_path: tuple[str, ...] = Field(default_factory=tuple)
    page_number: int | None = None
    source_uri: str = ""

    text_hash: str
    retrieved_at: str
    policy_version: str


class AnswerEnvelope(BaseModel):
    """Typed, validated, deeply-frozen grounded-answer container (build plan §7.9).

    Single-corpus / single-iteration MVP slice: ``iterations`` and ``tool_calls``
    are fixed at 1 because E-013 runs on the one-pass Fast Path. The model is
    validated so the following invariants always hold:

    * ``Claim``/``Citation`` are frozen;
    * every ``Claim.evidence_ids`` entry and every ``Citation.evidence_id``
      resolves to an ``evidence`` snapshot (no dangling citation);
    * ``abstained is True`` ⇒ ``claims == []``, ``evidence == []``,
      ``completeness == insufficient``, ``stop_reason == no_evidence``;
    * ``completeness == insufficient`` ⇒ ``abstained is True``.
    """

    model_config = ConfigDict(frozen=True)

    request_id: str
    session_id: str

    answer_markdown: str

    claims: tuple[Claim, ...] = Field(default_factory=tuple)
    evidence: tuple[SnapshotEvidence, ...] = Field(default_factory=tuple)
    citations: tuple[Citation, ...] = Field(default_factory=tuple)

    completeness: Completeness
    confidence: Confidence

    missing_aspects: tuple[str, ...] = Field(default_factory=tuple)
    limitations: tuple[str, ...] = Field(default_factory=tuple)

    corpora_used: tuple[str, ...] = Field(default_factory=tuple)
    iterations: int = 1
    tool_calls: int = 1

    # M3 quality-iteration metadata (build plan §7.8 / §14). Optional: the E-013
    # envelope is still valid without it. Does not affect the abstain/insufficient
    # lock in `_lock_state`.
    gap_rounds: int = 1
    coverage: SufficiencyResult | None = None

    # E-021 conflict report (optional, backward-compatible). When the resolver
    # reports CONTRADICTED the envelope's completeness is forced to "conflicted"
    # (see `_lock_state`). Absent for all pre-E-021 envelopes.
    conflict_report: ConflictReport | None = None

    stop_reason: str
    abstained: bool

    @model_validator(mode="after")
    def _lock_state(self) -> "AnswerEnvelope":
        evidence_ids = {e.evidence_id for e in self.evidence}
        # No dangling citation: every claim's and every citation's evidence id
        # must resolve to a snapshot in this envelope.
        for claim in self.claims:
            unknown = [eid for eid in claim.evidence_ids if eid not in evidence_ids]
            if unknown:
                raise ValueError(f"claim {claim.claim_id!r} cites unknown evidence ids: {unknown}")
        for cit in self.citations:
            if cit.evidence_id not in evidence_ids:
                raise ValueError(
                    f"citation #{cit.index} cites unknown evidence id: {cit.evidence_id!r}"
                )
        # Abstain state is locked, including the stop reason.
        if self.abstained:
            if self.claims:
                raise ValueError("abstained envelope must carry no claims")
            if self.evidence:
                raise ValueError("abstained envelope must carry no evidence")
            if self.completeness != "insufficient":
                raise ValueError("abstained envelope completeness must be 'insufficient'")
            if self.stop_reason != "no_evidence":
                raise ValueError("abstained envelope stop_reason must be 'no_evidence'")
        if self.completeness == "insufficient" and not self.abstained:
            raise ValueError("completeness 'insufficient' requires abstained=True")
        if self.stop_reason == "no_evidence" and not self.abstained:
            raise ValueError("stop_reason 'no_evidence' requires abstained=True")
        # E-021 conflict override (fail-closed): a contradicted report MUST be
        # paired with ``completeness == "conflicted"`` (the answer must enumerate
        # both sources/times). The ChatService path sets this via a contradicted
        # coverage; this check guarantees the invariant even for envelopes built
        # directly. It does not alter the existing abstain / insufficient lock
        # above, and only a non-abstained envelope can be conflicted.
        if (
            self.conflict_report is not None
            and self.conflict_report.conflict_status == ConflictStatus.CONTRADICTED
            and not self.abstained
            and self.completeness != "conflicted"
        ):
            raise ValueError(
                "envelope with a CONTRADICTED conflict_report must have "
                "completeness == 'conflicted'"
            )
        return self
