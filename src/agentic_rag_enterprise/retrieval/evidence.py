"""Evidence snapshot builder (build plan §12.8 / retrieval pipeline).

Converts an authorized ``(RetrievalHit, AuthorizedParent)`` pair, plus the
runtime :class:`SecurityContext` and the control-plane
:class:`~agentic_rag_enterprise.domain.document.SourceDocument` provenance, into
an immutable :class:`~agentic_rag_enterprise.domain.evidence.Evidence` snapshot.

The snapshot captures the *body* of the authorized parent (the grounded context
the model will cite), not a live link back to a document that may later be
deleted or ACL-tightened. Provenance (source uri/filename, authority, effective
dates, policy version) is pulled from the control-plane
:class:`SourceDocument` so the snapshot is faithful even though the Qdrant
payload does not carry those fields. Where the control-plane document is
unavailable, conservative defaults are used.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from hashlib import sha256
from typing import Optional

from agentic_rag_enterprise.domain.document import SourceDocument
from agentic_rag_enterprise.domain.evidence import Evidence
from agentic_rag_enterprise.domain.security import SecurityContext
from agentic_rag_enterprise.retrieval.deduplication import DedupCandidate
from agentic_rag_enterprise.retrieval.models import AuthorizedParent, RetrievalHit


class EvidenceBuilder:
    """Builds immutable :class:`Evidence` snapshots from retrieval results."""

    def build(
        self,
        hit: RetrievalHit,
        parent: AuthorizedParent,
        ctx: SecurityContext,
        *,
        query: str,
        iteration: int = 0,
        plan_step_id: Optional[str] = None,
        source_document: Optional[SourceDocument] = None,
    ) -> Evidence:
        """Construct one evidence snapshot for an authorized (hit, parent)."""
        sd = source_document

        provenance_uri = sd.source_uri if sd else f"inline://{hit.document_id}"
        provenance_filename = sd.source_filename if sd else hit.document_id
        authority = sd.authority_level if sd else 50
        acl_policy_id = sd.acl_policy_id if sd else "unknown"
        effective_from = sd.effective_from if sd else None
        effective_to = sd.effective_to if sd else None

        page_number = parent.metadata.get("page_number")
        if not isinstance(page_number, int):
            page_number = None

        body = parent.content or hit.text
        now = datetime.now(timezone.utc)

        return Evidence(
            evidence_id=uuid.uuid4().hex,
            tenant_id=hit.tenant_id,
            corpus_id=hit.corpus_id,
            document_id=hit.document_id,
            document_version=hit.document_version,
            source_uri=provenance_uri,
            source_filename=provenance_filename,
            parent_id=hit.parent_id or None,
            child_chunk_id=hit.chunk_id or None,
            page_number=page_number,
            section_path=tuple(parent.section_path) or tuple(hit.section_path),
            start_offset=None,
            end_offset=None,
            text=body,
            text_hash=sha256(body.encode("utf-8")).hexdigest(),
            retrieval_query=query,
            retrieval_score=hit.score,
            rerank_score=None,
            authority_level=authority,
            effective_from=effective_from,
            effective_to=effective_to,
            deprecated=False,
            retrieved_at=now,
            acl_policy_id=acl_policy_id,
            policy_version=ctx.policy_version,
            retrieval_iteration=iteration,
            plan_step_id=plan_step_id,
        )

    def build_from_candidate(
        self,
        candidate: DedupCandidate,
        parent: AuthorizedParent,
        ctx: SecurityContext,
        *,
        source_document: Optional[SourceDocument] = None,
    ) -> Evidence:
        """Convenience wrapper that reads query/iteration from a dedup candidate."""
        primary_ctx = candidate.primary_context
        return self.build(
            candidate.hit,
            parent,
            ctx,
            query=primary_ctx.query,
            iteration=primary_ctx.iteration,
            plan_step_id=primary_ctx.plan_step_id,
            source_document=source_document,
        )
