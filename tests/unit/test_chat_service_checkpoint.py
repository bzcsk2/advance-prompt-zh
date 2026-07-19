"""Unit tests for E-023 ChatService checkpoint + resume re-authorization.

Asserts the core contract guarantees:

* A run resumed from a completed (or mid-loop) checkpoint produces the SAME
  ``AnswerEnvelope`` as an uninterrupted run (determinism, invariant 4).
* On resume, evidence the principal can no longer read is DROPPED (fail-closed)
  and never re-surfaces (invariant 1 — "ACL 收紧不因旧 Cache/Checkpoint 泄露");
  an audit finding is recorded for the drop.
* Resume is refused (``ResumeAuthError``) when the principal differs, the
  ``policy_version`` changed, or the corpus is no longer discoverable.
* A finished run's checkpoint is marked ``completed``.

Hermetic: a query-keyed fake retriever, a deterministic judge, an in-memory
MetadataStore, and an ``InMemoryCorpusRegistry``.
"""

from __future__ import annotations

import re
from datetime import datetime

import pytest

from agentic_rag_enterprise.answer.envelope import Claim
from agentic_rag_enterprise.corpus.registry import InMemoryCorpusRegistry
from agentic_rag_enterprise.domain.corpus import CorpusConfig
from agentic_rag_enterprise.domain.evidence import Evidence as SnapshotEvidence
from agentic_rag_enterprise.domain.security import SecurityContext
from agentic_rag_enterprise.judge.deterministic_coverage_judge import (
    DeterministicCoverageJudge,
)
from agentic_rag_enterprise.judge.models import RequiredFact
from agentic_rag_enterprise.judge.query_fact_extractor import make_required_fact
from agentic_rag_enterprise.retrieval.fast_path import FastPathBackendError
from agentic_rag_enterprise.retrieval.models import CorpusNotDiscoverableError
from agentic_rag_enterprise.services.chat_service import ChatService
from agentic_rag_enterprise.services.claims_schema import ClaimExtraction
from agentic_rag_enterprise.storage.checkpoint_store import (
    CHECKPOINT_COMPLETED,
    ResumeAuthError,
)
from agentic_rag_enterprise.storage.metadata_store import MetadataStore
from agentic_rag_enterprise.storage.vector_store import DenseEncoder, SparseEncoder
from tests.fixtures import FakeDenseEncoder, FakeSparseEncoder


def _corpus(corpus_id: str = "eng", tenant_id: str = "t1") -> CorpusConfig:
    return CorpusConfig(
        corpus_id=corpus_id,
        tenant_id=tenant_id,
        name="Eng",
        description="",
        domain="",
        owner="",
        source_type="wiki",
        capability_ids=[],
        enabled=True,
        searchable=True,
        security_policy_id="p",
        default_security_level="internal",
        created_at=datetime(2024, 1, 1),
        updated_at=datetime(2024, 1, 1),
    )


def _registry(corpus_id: str = "eng", tenant_id: str = "t1") -> InMemoryCorpusRegistry:
    return InMemoryCorpusRegistry([_corpus(corpus_id, tenant_id)])


def _evidence(
    evidence_id: str,
    text: str,
    tenant_id: str = "t1",
    corpus_id: str = "eng",
    document_id: str = "d1",
    document_version: str = "v1",
    text_hash: str | None = None,
) -> SnapshotEvidence:
    return SnapshotEvidence(
        evidence_id=evidence_id,
        tenant_id=tenant_id,
        corpus_id=corpus_id,
        document_id=document_id,
        document_version=document_version,
        source_uri="inline://d1",
        source_filename="d1.md",
        text=text,
        text_hash=text_hash if text_hash is not None else f"h-{evidence_id}",
        retrieval_query="q",
        authority_level=50,
        retrieved_at=datetime(2024, 1, 1),
        acl_policy_id="default",
        policy_version="1.0",
        retrieval_iteration=0,
    )


def _ctx(
    tenant_id: str = "t1", user_id: str = "u1", policy_version: str = "1.0"
) -> SecurityContext:
    return SecurityContext(
        request_id="r1",
        session_id="s1",
        tenant_id=tenant_id,
        user_id=user_id,
        policy_version=policy_version,
    )


class _FakeLoopRetriever:
    """Query-keyed fake retriever; ``fault_query`` raises once then succeeds."""

    def __init__(self, evidence_map: dict[str, object], *, fault_query: str | None = None) -> None:
        self._map = evidence_map
        self._fault_query = fault_query
        self._faulted = False
        self.calls: list[tuple[str, int]] = []

    def retrieve_evidence(
        self,
        ctx: SecurityContext,
        query: str,
        corpus: CorpusConfig,
        top_k: object = None,
        *,
        dense_encoder: DenseEncoder,
        sparse_encoder: SparseEncoder,
        iteration: int = 0,
        plan_step_id: object = None,
    ) -> list[SnapshotEvidence]:
        self.calls.append((query, iteration))
        if corpus.tenant_id != ctx.tenant_id:
            raise CorpusNotDiscoverableError(
                f"corpus {corpus.corpus_id} not discoverable for {ctx.tenant_id}"
            )
        if self._fault_query is not None and query == self._fault_query and not self._faulted:
            self._faulted = True
            raise RuntimeError("simulated crash")
        payload = self._map.get(query, [])
        if isinstance(payload, Exception):
            raise payload
        return list(payload)


class _LoopModel:
    """One claim per ``[evidence_id]`` marker in the synthesized answer."""

    def invoke(self, messages, **kwargs):
        return ""

    def with_structured_output(self, schema, **kwargs):
        return self._Wrapper(self, schema)

    class _Wrapper:
        def __init__(self, model, schema):
            self._model = model
            self._schema = schema

        def invoke(self, messages, **kwargs):
            blob = "\n".join(m.get("content", "") for m in messages)
            claims = []
            for seg in re.split(r"(?=\[[A-Za-z0-9_-]+\])", blob):
                m = re.match(r"\[([A-Za-z0-9_-]+)\]\s*[^\n]*\n(.*?)(?:\n\n|\Z)", seg, re.DOTALL)
                if not m:
                    continue
                text = m.group(2).strip()
                if text:
                    claims.append(
                        Claim(claim_id=f"c{len(claims)}", text=text, evidence_ids=(m.group(1),))
                    )
            return ClaimExtraction(draft_answer="\n".join(c.text for c in claims), claims=claims)


def _service(
    retriever: _FakeLoopRetriever,
    *,
    mstore: MetadataStore | None,
    registry: InMemoryCorpusRegistry,
) -> ChatService:
    return ChatService(
        retriever=retriever,
        dense_encoder=FakeDenseEncoder(),
        sparse_encoder=FakeSparseEncoder(),
        model=_LoopModel(),
        resolve_corpus=lambda cid: _corpus(corpus_id=cid, tenant_id="t1"),
        metadata_store=mstore,
        judge=DeterministicCoverageJudge(),
        registry=registry,
    )


def _facts(*descs: str) -> list[RequiredFact]:
    return [make_required_fact(d) for d in descs]


def _mstore() -> MetadataStore:
    import os
    import tempfile

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    return MetadataStore(path)


# --- Determinism: resume == uninterrupted -----------------------------------


def _vacation_scenario() -> dict[str, object]:
    return {
        "q": [_evidence("e1", "The vacation policy grants 20 days paid leave.")],
        "request time off": [
            _evidence("e2", "Employees request time off via the HR portal."),
        ],
    }


def test_resume_from_completed_checkpoint_equals_uninterrupted() -> None:
    # Uninterrupted reference run.
    ref_service = _service(
        _FakeLoopRetriever(_vacation_scenario()), mstore=None, registry=_registry()
    )
    ref = ref_service.answer_with_iteration(
        "q",
        _ctx(),
        "eng",
        max_rounds=5,
        judge=DeterministicCoverageJudge(),
        required_facts=_facts("vacation policy", "request time off"),
    )

    # A full run that checkpoints, then a resume of the completed checkpoint.
    mstore = _mstore()
    _seed_active_document(mstore, "d1")
    svc = _service(_FakeLoopRetriever(_vacation_scenario()), mstore=mstore, registry=_registry())
    svc.answer_with_iteration(
        "q",
        _ctx(),
        "eng",
        max_rounds=5,
        run_id="R1",
        judge=DeterministicCoverageJudge(),
        required_facts=_facts("vacation policy", "request time off"),
    )
    ck = mstore.load_run_checkpoint("R1")
    assert ck is not None
    # The finished checkpoint is marked completed.
    row = mstore._conn.execute(
        "SELECT status FROM run_checkpoints WHERE run_id=?", ("R1",)
    ).fetchone()
    assert row["status"] == CHECKPOINT_COMPLETED

    resumed = svc.resume_run("R1", _ctx())
    _assert_same_envelope(resumed, ref)


def test_resume_from_mid_loop_crash_continues_to_same_answer() -> None:
    # First pass crashes on the round-1 gap query, leaving a `running` checkpoint
    # at round_index=1 (evidence from round 0 only). Resume continues and must
    # reach the same sufficient answer as an uninterrupted run.
    scenario = _vacation_scenario()
    mstore = _mstore()
    _seed_active_document(mstore, "d1")

    crash_retriever = _FakeLoopRetriever(scenario, fault_query="request time off")
    svc = _service(crash_retriever, mstore=mstore, registry=_registry())
    with pytest.raises(FastPathBackendError):  # fault propagates as a backend error
        svc.answer_with_iteration(
            "q",
            _ctx(),
            "eng",
            max_rounds=5,
            run_id="R2",
            judge=DeterministicCoverageJudge(),
            required_facts=_facts("vacation policy", "request time off"),
        )
    # The crash leaves a running checkpoint at round_index 1 with only round-0 evidence.
    ck = mstore.load_run_checkpoint("R2")
    assert ck is not None
    assert ck.round_index == 1
    assert {e.evidence_id for e in ck.evidence} == {"e1"}

    # Resume: the same retriever now succeeds on the gap query.
    resumed = svc.resume_run("R2", _ctx())
    assert resumed.abstained is False
    assert resumed.completeness == "complete"
    assert {e.evidence_id for e in resumed.evidence} == {"e1", "e2"}

    # Reference uninterrupted run for identity.
    ref = _service(
        _FakeLoopRetriever(scenario), mstore=None, registry=_registry()
    ).answer_with_iteration(
        "q",
        _ctx(),
        "eng",
        max_rounds=5,
        judge=DeterministicCoverageJudge(),
        required_facts=_facts("vacation policy", "request time off"),
    )
    _assert_same_envelope(resumed, ref)


# --- Re-auth drop without leak ----------------------------------------------


def test_resume_drops_revoked_evidence_after_acl_tighten() -> None:
    # Two documents, each contributing one evidence. After round 0, tighten doc2's
    # ACL so u1 can no longer read it; resume must drop e2 and only answer from e1.
    scenario = {
        "q": [
            _evidence("e1", "The vacation policy grants 20 days paid leave.", document_id="d1"),
            _evidence("e2", "The bonus policy grants a 10% bonus.", document_id="d2"),
        ],
    }
    mstore = _mstore()
    svc = _service(_FakeLoopRetriever(scenario), mstore=mstore, registry=_registry())

    # Seed BOTH documents as active in the MetadataStore (so re-auth can resolve
    # their current ACL). d1 stays public/tenant; d2 will be tightened.
    _seed_active_document(mstore, "d1")
    _seed_active_document(mstore, "d2")

    svc.answer_with_iteration(
        "q",
        _ctx(),
        "eng",
        max_rounds=1,
        run_id="R3",
        judge=DeterministicCoverageJudge(),
        required_facts=_facts("vacation policy", "bonus policy"),
    )

    # Tighten d2's ACL: only "other" may read it now.
    mstore.update_document_acl(
        "t1",
        "eng",
        "d2",
        "v1",
        security_level="public",
        acl_scope="restricted",
        allowed_user_ids=["other"],
        allowed_group_ids=[],
        denied_user_ids=[],
        denied_group_ids=[],
    )

    resumed = svc.resume_run("R3", _ctx())
    # e2 must never re-surface.
    assert {e.evidence_id for e in resumed.evidence} == {"e1"}
    assert all(e.document_id != "d2" for e in resumed.evidence)
    # The drop is recorded as an audit finding.
    findings = _control_plane_findings(mstore)
    assert any(f["kind"] == "resume_evidence_revoked" for f in findings)


def test_resume_refuses_on_principal_mismatch() -> None:
    mstore = _mstore()
    svc = _service(_FakeLoopRetriever(_vacation_scenario()), mstore=mstore, registry=_registry())
    svc.answer_with_iteration(
        "q",
        _ctx(),
        "eng",
        max_rounds=1,
        run_id="R4",
        judge=DeterministicCoverageJudge(),
        required_facts=_facts("vacation policy"),
    )
    # A different user/session attempts to resume -> refused (fail closed).
    with pytest.raises(ResumeAuthError):
        svc.resume_run("R4", _ctx(user_id="u9", tenant_id="t1"))


def test_resume_refuses_on_policy_version_change() -> None:
    mstore = _mstore()
    svc = _service(_FakeLoopRetriever(_vacation_scenario()), mstore=mstore, registry=_registry())
    svc.answer_with_iteration(
        "q",
        _ctx(),
        "eng",
        max_rounds=1,
        run_id="R5",
        judge=DeterministicCoverageJudge(),
        required_facts=_facts("vacation policy"),
    )
    # The authorization basis is stale -> abort.
    with pytest.raises(ResumeAuthError):
        svc.resume_run("R5", _ctx(user_id="u1", policy_version="2.0"))


def test_resume_refuses_when_corpus_undiscoverable() -> None:
    mstore = _mstore()
    svc = _service(_FakeLoopRetriever(_vacation_scenario()), mstore=mstore, registry=_registry())
    svc.answer_with_iteration(
        "q",
        _ctx(),
        "eng",
        max_rounds=1,
        run_id="R6",
        judge=DeterministicCoverageJudge(),
        required_facts=_facts("vacation policy"),
    )
    # A registry in which "eng" is not discoverable for the principal.
    foreign_registry = InMemoryCorpusRegistry([_corpus(corpus_id="other", tenant_id="t2")])
    svc2 = _service(
        _FakeLoopRetriever(_vacation_scenario()), mstore=mstore, registry=foreign_registry
    )
    with pytest.raises(ResumeAuthError):
        svc2.resume_run("R6", _ctx())


def test_resume_unknown_run_id_raises() -> None:
    mstore = _mstore()
    svc = _service(_FakeLoopRetriever(_vacation_scenario()), mstore=mstore, registry=_registry())
    with pytest.raises(ResumeAuthError):
        svc.resume_run("nope", _ctx())


# --- Helpers ---------------------------------------------------------------


def _assert_same_envelope(a, b) -> None:
    assert a.completeness == b.completeness
    assert a.confidence == b.confidence
    assert a.stop_reason == b.stop_reason
    assert {e.evidence_id for e in a.evidence} == {e.evidence_id for e in b.evidence}
    assert a.answer_markdown == b.answer_markdown
    if a.coverage is not None and b.coverage is not None:
        assert a.coverage.overall_status == b.coverage.overall_status


def _seed_active_document(mstore: MetadataStore, document_id: str) -> None:
    from datetime import timezone

    from agentic_rag_enterprise.domain.document import SourceDocument
    from agentic_rag_enterprise.domain.ingestion import DocumentStatus

    now = datetime(2024, 1, 1, tzinfo=timezone.utc)
    mstore._conn.execute(  # noqa: SLF001 - fixture seed
        """
        INSERT OR IGNORE INTO corpus_registry (
            corpus_id, tenant_id, name, description, created_at, updated_at
        ) VALUES (?, ?, 'corpus', '', ?, ?)
        """,
        ("eng", "t1", now.isoformat(), now.isoformat()),
    )
    doc = SourceDocument(
        document_id=document_id,
        tenant_id="t1",
        corpus_id="eng",
        source_uri=f"inline://{document_id}",
        source_connector="file",
        title=document_id,
        source_filename=f"{document_id}.md",
        mime_type="text/markdown",
        version="v1",
        content_hash="seed",
        status=DocumentStatus.ACTIVE,
        authority_level=50,
        deprecated=False,
        acl_policy_id="default",
        security_level="public",
        acl_scope="tenant",
        allowed_user_ids=["u1"],
        allowed_group_ids=["g1"],
        denied_user_ids=[],
        denied_group_ids=[],
        parser_name="markdown",
        parser_version="1.0",
        chunking_version="1.0",
        embedding_model="fake",
        embedding_version="1.0",
        discovered_at=now,
        indexed_at=now,
        last_synced_at=now,
    )
    mstore.upsert_document(doc)


def _control_plane_findings(mstore: MetadataStore) -> list[dict]:
    rows = mstore._conn.execute("SELECT kind, detail FROM reconciliation_findings").fetchall()
    return [dict(r) for r in rows]
