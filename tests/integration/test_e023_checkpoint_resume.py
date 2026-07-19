"""Integration tests for E-023 checkpoint + resume on the default app.

Unlike the unit tests (which hand-build a ``MetadataStore``), this suite drives
the REAL default-app container: documents are ingested through the genuine
ingestion pipeline into the shared Metadata DB + Qdrant, and the
:class:`ChatService` re-authorizes resumed evidence against the REAL active
documents. The retriever / model are hermetic fakes (permitted by the contract —
no LLM / network), but every persistence and re-authorization code path is the
production one.

Asserts the E-023 invariants end-to-end:

* the Metadata DB is the single source of truth for the checkpoint (invariant 5);
* a run resumed from a completed checkpoint equals the uninterrupted run
  (determinism, invariant 4);
* after an ACL tighten, resumed evidence the principal lost access to is DROPPED
  and never leaks, with an audit finding recorded (invariant 1 — "ACL 收紧不因旧
  Cache/Checkpoint 泄露");
* a mid-loop crash leaves a resumable ``running`` checkpoint that continues to
  the same answer.
"""

from __future__ import annotations

import os
import re
import tempfile
from datetime import datetime

import pytest

from agentic_rag_enterprise.answer.envelope import Claim
from agentic_rag_enterprise.domain.corpus import CorpusConfig
from agentic_rag_enterprise.domain.evidence import Evidence as SnapshotEvidence
from agentic_rag_enterprise.domain.security import SecurityContext
from agentic_rag_enterprise.judge.deterministic_coverage_judge import (
    DeterministicCoverageJudge,
)
from agentic_rag_enterprise.judge.models import RequiredFact, SufficiencyResult
from agentic_rag_enterprise.judge.query_fact_extractor import make_required_fact
from agentic_rag_enterprise.retrieval.fast_path import (
    FastPathBackendError,
    FastPathResult,
    FastPathStopReason,
    FastPathSufficiency,
)
from agentic_rag_enterprise.retrieval.models import CorpusNotDiscoverableError
from agentic_rag_enterprise.security.policy import ResourceAcl
from agentic_rag_enterprise.services.chat_service import ChatService
from agentic_rag_enterprise.services.claims_schema import ClaimExtraction
from agentic_rag_enterprise.services.container import (
    DefaultServiceContainer,
    get_default_container,
    reset_default_container,
)
from agentic_rag_enterprise.storage.checkpoint_store import (
    CHECKPOINT_RUNNING,
    CheckpointIdentityConflict,
    RunCheckpoint,
)
from agentic_rag_enterprise.storage.vector_store import DenseEncoder, SparseEncoder
from tests.fixtures import FakeDenseEncoder, FakeSparseEncoder


@pytest.fixture(autouse=True)
def _fresh_container() -> None:
    # E-023 P1-2 hermeticity fix: each test gets its own temp metadata DB file
    # (the default container would otherwise share the production ``metadata.db``
    # across tests, leaking ingested docs / run checkpoints between them).
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    reset_default_container(metadata_db_path=path)
    yield
    reset_default_container()
    try:
        os.unlink(path)
    except OSError:
        pass


def _ctx() -> SecurityContext:
    return SecurityContext(
        request_id="r1",
        session_id="s1",
        tenant_id="t1",
        user_id="u1",
        policy_version="1.0",
    )


def _eng_corpus() -> CorpusConfig:
    return CorpusConfig(
        corpus_id="eng",
        tenant_id="t1",
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


def _evidence(evidence_id: str, text: str, document_id: str) -> SnapshotEvidence:
    return SnapshotEvidence(
        evidence_id=evidence_id,
        tenant_id="t1",
        corpus_id="eng",
        document_id=document_id,
        document_version="v1",
        source_uri=f"inline://{document_id}",
        source_filename=f"{document_id}.md",
        text=text,
        text_hash=f"h-{evidence_id}",
        retrieval_query="q",
        authority_level=50,
        retrieved_at=datetime(2024, 1, 1),
        acl_policy_id="default",
        policy_version="1.0",
        retrieval_iteration=0,
    )


def _public_acl() -> ResourceAcl:
    return ResourceAcl(
        tenant_id="t1",
        security_level="public",
        acl_scope="tenant",
        allowed_user_ids=["u1"],
        allowed_group_ids=[],
        denied_user_ids=[],
        denied_group_ids=[],
    )


class _FakeRetriever:
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


def _ingest(container: DefaultServiceContainer, document_id: str, content: str) -> None:
    result = container.ingest(
        tenant_id="t1",
        corpus_id="eng",
        document_id=document_id,
        document_version="v1",
        content=content,
        acl=_public_acl(),
        job_id=f"job-{document_id}",
        security_level="public",
    )
    assert result.status.value in ("indexed", "already_indexed"), result


def _service(container: DefaultServiceContainer, retriever: _FakeRetriever) -> ChatService:
    # Real shared MetadataStore + real corpus registry from the container; only
    # retrieval / synthesis are faked (hermetic, contract-permitted).
    return ChatService(
        retriever=retriever,
        dense_encoder=FakeDenseEncoder(),
        sparse_encoder=FakeSparseEncoder(),
        model=_LoopModel(),
        resolve_corpus=lambda cid: _eng_corpus(),
        metadata_store=container.metadata_store,
        judge=DeterministicCoverageJudge(),
        registry=container.corpus_registry,
    )


def _facts(*descs: str) -> list[RequiredFact]:
    return [make_required_fact(d) for d in descs]


def _vacation_map() -> dict[str, object]:
    return {
        "q": [_evidence("e1", "The vacation policy grants 20 days paid leave.", "d1")],
        "bonus structure": [_evidence("e2", "The bonus policy grants a 10% bonus.", "d2")],
    }


def _assert_same(a, b) -> None:
    assert a.completeness == b.completeness
    assert a.confidence == b.confidence
    assert a.stop_reason == b.stop_reason
    assert {e.evidence_id for e in a.evidence} == {e.evidence_id for e in b.evidence}
    assert a.answer_markdown == b.answer_markdown
    if a.coverage is not None and b.coverage is not None:
        assert a.coverage.overall_status == b.coverage.overall_status


def test_checkpoint_persists_on_shared_metadata_db_and_resume_matches() -> None:
    container = get_default_container()
    _ingest(container, "d1", "The vacation policy grants 20 days paid leave.")
    _ingest(container, "d2", "The bonus policy grants a 10% bonus.")

    svc = _service(container, _FakeRetriever(_vacation_map()))

    ref = svc.answer_with_iteration(
        "q",
        _ctx(),
        "eng",
        max_rounds=5,
        judge=DeterministicCoverageJudge(),
        required_facts=_facts("vacation policy", "bonus structure"),
    )

    svc.answer_with_iteration(
        "q",
        _ctx(),
        "eng",
        max_rounds=5,
        run_id="R1",
        judge=DeterministicCoverageJudge(),
        required_facts=_facts("vacation policy", "bonus structure"),
    )

    # The checkpoint lives on the REAL shared Metadata DB (invariant 5).
    row = container.metadata_store._conn.execute(
        "SELECT status FROM run_checkpoints WHERE run_id=?", ("R1",)
    ).fetchone()
    assert row is not None
    assert row["status"] == "completed"

    resumed = svc.resume_run("R1", _ctx())
    _assert_same(resumed, ref)


def test_resume_drops_revoked_evidence_after_acl_tighten_no_leak() -> None:
    container = get_default_container()
    _ingest(container, "d1", "The vacation policy grants 20 days paid leave.")
    _ingest(container, "d2", "The bonus policy grants a 10% bonus.")

    svc = _service(container, _FakeRetriever(_vacation_map()))
    svc.answer_with_iteration(
        "q",
        _ctx(),
        "eng",
        max_rounds=5,
        run_id="R2",
        judge=DeterministicCoverageJudge(),
        required_facts=_facts("vacation policy", "bonus structure"),
    )

    # Tighten d2's ACL so u1 can no longer read it (control-plane ACL tighten).
    mstore = container.metadata_store
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

    resumed = svc.resume_run("R2", _ctx())
    # The revoked evidence must never re-surface.
    assert {e.evidence_id for e in resumed.evidence} == {"e1"}
    assert all(e.document_id != "d2" for e in resumed.evidence)
    # The drop is auditable.
    findings = [
        dict(r) for r in mstore._conn.execute("SELECT kind FROM reconciliation_findings").fetchall()
    ]
    assert any(f["kind"] == "resume_evidence_revoked" for f in findings)


def test_mid_loop_crash_leaves_resumable_checkpoint() -> None:
    container = get_default_container()
    _ingest(container, "d1", "The vacation policy grants 20 days paid leave.")
    _ingest(container, "d2", "The bonus policy grants a 10% bonus.")

    # First pass crashes on the gap query, leaving a `running` checkpoint.
    crash_retriever = _FakeRetriever(_vacation_map(), fault_query="bonus structure")
    svc = _service(container, crash_retriever)
    with pytest.raises(FastPathBackendError):
        svc.answer_with_iteration(
            "q",
            _ctx(),
            "eng",
            max_rounds=5,
            run_id="R3",
            judge=DeterministicCoverageJudge(),
            required_facts=_facts("vacation policy", "bonus structure"),
        )
    ck = container.metadata_store.load_run_checkpoint("R3")
    assert ck is not None
    assert ck.round_index == 1  # only round 0 had completed before the crash
    assert {e.evidence_id for e in ck.evidence} == {"e1"}

    # Resume continues and reaches the same sufficient answer.
    resumed = svc.resume_run("R3", _ctx())
    assert resumed.abstained is False
    assert resumed.completeness == "complete"
    assert {e.evidence_id for e in resumed.evidence} == {"e1", "e2"}

    ref = _service(container, _FakeRetriever(_vacation_map())).answer_with_iteration(
        "q",
        _ctx(),
        "eng",
        max_rounds=5,
        judge=DeterministicCoverageJudge(),
        required_facts=_facts("vacation policy", "bonus structure"),
    )
    _assert_same(resumed, ref)


# --------------------------------------------------------------------------- #
# E-023 P1-2: checkpoint must survive a real process restart (file-backed DB)
# --------------------------------------------------------------------------- #
def test_checkpoint_persists_across_process_restart(tmp_path: "object") -> None:
    """A checkpoint written by one container/process must be recoverable by a
    brand-new container that reopens the SAME metadata DB file — proving the
    default container no longer uses a vanishing random tempfile (P1-2 fix)."""
    import os

    db = str(tmp_path / "ck_cross_process.db")
    # Process A: a REAL default container writing to a STABLE file.
    a = DefaultServiceContainer(metadata_db_path=db)
    a.ingest(
        tenant_id="t1",
        corpus_id="eng",
        document_id="d1",
        document_version="v1",
        content="The vacation policy grants 20 days paid leave.",
        acl=_public_acl(),
        job_id="job-proc-a",
        security_level="public",
    )
    a.ingest(
        tenant_id="t1",
        corpus_id="eng",
        document_id="d2",
        document_version="v1",
        content="The bonus policy grants a 10% bonus.",
        acl=_public_acl(),
        job_id="job-proc-a2",
        security_level="public",
    )
    svc_a = _service(a, _FakeRetriever(_vacation_map()))
    ref = svc_a.answer_with_iteration(
        "q",
        _ctx(),
        "eng",
        max_rounds=5,
        run_id="XP",
        judge=DeterministicCoverageJudge(),
        required_facts=_facts("vacation policy", "bonus structure"),
    )
    # The checkpoint lives in the FILE, not process memory — it survives teardown.
    assert os.path.exists(db)

    # Process B: a brand-new container + store reopening the SAME file.
    b = DefaultServiceContainer(metadata_db_path=db)
    svc_b = _service(b, _FakeRetriever(_vacation_map()))
    resumed = svc_b.resume_run("XP", _ctx())
    _assert_same(resumed, ref)


# --------------------------------------------------------------------------- #
# E-023 P1-3: every completed round persists, so a SECOND crash resumes from
# the latest round — not round 0 — and the run still completes identically.
# --------------------------------------------------------------------------- #
def _three_fact_map() -> dict[str, object]:
    return {
        "q": [_evidence("e1", "The vacation policy grants 20 days paid leave.", "d1")],
        "bonus structure": [_evidence("e2", "The bonus policy grants a 10% bonus.", "d2")],
        "remote work policy": [
            _evidence("e3", "The remote work policy allows two remote days.", "d3")
        ],
    }


class _CrashRetriever:
    """Fake retriever that raises ONCE per query in ``crash_queries`` (simulating a
    mid-loop crash), then returns the evidence on later calls so a resume past the
    crash point succeeds. Crashing once-per-query models a transient infra fault
    that is gone by the time the run is resumed."""

    def __init__(self, evidence_map: dict[str, object], *, crash_queries: set[str]) -> None:
        self._map = evidence_map
        self._crash = set(crash_queries)
        self._crashed: set[str] = set()

    def retrieve_evidence(
        self,
        ctx: "SecurityContext",
        query: str,
        corpus: "CorpusConfig",
        top_k: object = None,
        *,
        dense_encoder: "DenseEncoder",
        sparse_encoder: "SparseEncoder",
        iteration: int = 0,
        plan_step_id: object = None,
    ) -> list["SnapshotEvidence"]:
        if corpus.tenant_id != ctx.tenant_id:
            raise CorpusNotDiscoverableError(
                f"corpus {corpus.corpus_id} not discoverable for {ctx.tenant_id}"
            )
        if query in self._crash and query not in self._crashed:
            self._crashed.add(query)
            raise RuntimeError("simulated crash")
        payload = self._map.get(query, [])
        if isinstance(payload, Exception):
            raise payload
        return list(payload)


def test_double_crash_resumes_from_latest_round_not_round_zero() -> None:
    container = get_default_container()
    _ingest(container, "d1", "The vacation policy grants 20 days paid leave.")
    _ingest(container, "d2", "The bonus policy grants a 10% bonus.")
    _ingest(container, "d3", "The remote work policy allows two remote days.")
    facts = _facts("vacation policy", "bonus structure", "remote work policy")

    retr = _CrashRetriever(
        _three_fact_map(), crash_queries={"bonus structure", "remote work policy"}
    )
    svc = _service(container, retr)

    # First execution crashes on the round-1 gap query → leaves a running
    # checkpoint at round_index == 1 (only round 0 had completed).
    with pytest.raises(FastPathBackendError):
        svc.answer_with_iteration(
            "q",
            _ctx(),
            "eng",
            max_rounds=5,
            run_id="DC",
            judge=DeterministicCoverageJudge(),
            required_facts=facts,
        )
    ck1 = container.metadata_store.load_run_checkpoint("DC")
    assert ck1 is not None
    assert ck1.round_index == 1

    # Resume: re-runs round 1 (now succeeds because the fault is gone), then
    # crashes on the round-2 gap query → round 1 was persisted (round_index == 2).
    with pytest.raises(FastPathBackendError):
        svc.resume_run("DC", _ctx())
    ck2 = container.metadata_store.load_run_checkpoint("DC")
    assert ck2 is not None
    assert ck2.round_index == 2  # not 0 — persistence advances per completed round

    # Second resume: continues from round 2 and completes identically.
    resumed = svc.resume_run("DC", _ctx())
    assert resumed.abstained is False
    assert {e.evidence_id for e in resumed.evidence} == {"e1", "e2", "e3"}

    ref = _service(
        container, _CrashRetriever(_three_fact_map(), crash_queries=set())
    ).answer_with_iteration(
        "q",
        _ctx(),
        "eng",
        max_rounds=5,
        judge=DeterministicCoverageJudge(),
        required_facts=facts,
    )
    _assert_same(resumed, ref)


# --------------------------------------------------------------------------- #
# E-023 P1-4: after an ACL tighten, the resumed run recomputes its derived state
# from the SURVIVING evidence and never reuses the stale "sufficient" verdict or
# names the revoked source in the ConflictReport.
# --------------------------------------------------------------------------- #
def test_resume_recomputes_derived_state_after_revocation_no_stale_sufficient() -> None:
    container = get_default_container()
    _ingest(container, "d1", "The vacation policy grants 20 days paid leave.")
    _ingest(container, "d2", "The bonus policy grants a 10% bonus.")

    svc = _service(container, _FakeRetriever(_vacation_map()))
    svc.answer_with_iteration(
        "q",
        _ctx(),
        "eng",
        max_rounds=5,
        run_id="R4",
        judge=DeterministicCoverageJudge(),
        required_facts=_facts("vacation policy", "bonus structure"),
    )

    # Tighten d2's ACL so u1 can no longer read e2 (control-plane ACL tighten).
    mstore = container.metadata_store
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

    resumed = svc.resume_run("R4", _ctx())
    # The revoked evidence must never re-surface (invariant 1).
    assert {e.evidence_id for e in resumed.evidence} == {"e1"}
    # The stale "sufficient" verdict must NOT survive the revocation: with e2 gone
    # the run can no longer be sufficient/complete from the revoked data.
    assert resumed.coverage is not None
    assert resumed.coverage.overall_status != "sufficient"
    # The recomputed ConflictReport must not name the revoked source e2.
    if resumed.conflict_report is not None:
        named = {
            src.evidence_id
            for finding in resumed.conflict_report.findings
            for src in finding.sources
        }
        assert "e2" not in named


# --------------------------------------------------------------------------- #
# E-023 P1-5: a client-supplied run_id is bound to an immutable identity; reusing
# it under a DIFFERENT tenant / user / query is refused and the original row is
# left untouched. The same identity may UPDATE (re-checkpoint) without conflict.
# --------------------------------------------------------------------------- #
def _make_checkpoint(
    run_id: str,
    *,
    tenant: str = "t1",
    user: str = "u1",
    session: str = "s1",
    query: str = "q",
    corpus: str = "eng",
    policy: str = "1.0",
    round_index: int = 0,
) -> RunCheckpoint:
    return RunCheckpoint(
        run_id=run_id,
        tenant_id=tenant,
        user_id=user,
        session_id=session,
        policy_version=policy,
        query=query,
        corpus_id=corpus,
        max_rounds=5,
        required_facts=[],
        round_index=round_index,
        evidence=(),
        prior_queries=[query],
        seen_text_hashes=[],
        seen_doc_versions=[],
        retrieval_calls=1,
        gap_rounds=0,
        final_reason="ok",
        conflict_stop=False,
        coverage=None,
        final_report=None,
        final_evidence_ids=[],
        first_result=None,
    )


def _row_identity(mstore: "object", run_id: str) -> dict:
    return dict(
        mstore._conn.execute(
            "SELECT tenant_id, user_id, session_id, query, corpus_id, policy_version "
            "FROM run_checkpoints WHERE run_id=?",
            (run_id,),
        ).fetchone()
    )


def test_run_id_collision_rejected_across_tenant() -> None:
    mstore = get_default_container().metadata_store
    mstore.save_run_checkpoint(
        _make_checkpoint("C1", tenant="t1", user="u1", session="s1", query="q")
    )
    with pytest.raises(CheckpointIdentityConflict):
        mstore.save_run_checkpoint(
            _make_checkpoint("C1", tenant="t2", user="u1", session="s1", query="q")
        )
    assert _row_identity(mstore, "C1") == {
        "tenant_id": "t1",
        "user_id": "u1",
        "session_id": "s1",
        "query": "q",
        "corpus_id": "eng",
        "policy_version": "1.0",
    }


def test_run_id_collision_rejected_across_user() -> None:
    mstore = get_default_container().metadata_store
    mstore.save_run_checkpoint(
        _make_checkpoint("C2", tenant="t1", user="u1", session="s1", query="q")
    )
    with pytest.raises(CheckpointIdentityConflict):
        mstore.save_run_checkpoint(
            _make_checkpoint("C2", tenant="t1", user="u2", session="s1", query="q")
        )
    assert _row_identity(mstore, "C2") == {
        "tenant_id": "t1",
        "user_id": "u1",
        "session_id": "s1",
        "query": "q",
        "corpus_id": "eng",
        "policy_version": "1.0",
    }


def test_run_id_collision_rejected_across_query() -> None:
    mstore = get_default_container().metadata_store
    mstore.save_run_checkpoint(_make_checkpoint("C3", query="what is the vacation policy?"))
    with pytest.raises(CheckpointIdentityConflict):
        mstore.save_run_checkpoint(_make_checkpoint("C3", query="what is the bonus policy?"))
    assert _row_identity(mstore, "C3")["query"] == "what is the vacation policy?"


def test_run_id_same_identity_updates_state_without_conflict() -> None:
    mstore = get_default_container().metadata_store
    mstore.save_run_checkpoint(_make_checkpoint("C4", round_index=0))
    # Same identity → UPDATE (re-checkpoint), no conflict; the status column is
    # frozen by the complete/abort methods, not overwritten here.
    mstore.save_run_checkpoint(_make_checkpoint("C4", round_index=2))
    ck = mstore.load_run_checkpoint("C4")
    assert ck is not None
    assert ck.round_index == 2
    assert ck.status == CHECKPOINT_RUNNING


# --------------------------------------------------------------------------- #
# P1-3 residual 测试：completed 状态机 — 零调用 / 翻转验证
# --------------------------------------------------------------------------- #
class _FaultModel:
    """Model that raises on any call — used to prove resume does not invoke it."""

    def invoke(self, *args, **kwargs):
        pytest.fail("model.invoke should not be called on completed refusal resume")

    def with_structured_output(self, *args, **kwargs):
        pytest.fail("model.with_structured_output should not be called on completed refusal resume")


class _FaultJudge:
    """Judge that raises on any call — used to prove resume does not invoke it."""

    def judge(self, *, query, required_facts, evidence, timeout=None):
        pytest.fail("judge.judge should not be called on completed refusal resume")


class _FaultRetriever:
    """Retriever that raises on any call — used to prove resume does not invoke it."""

    def retrieve_evidence(
        self, ctx, query, corpus, top_k=None, *, dense_encoder, sparse_encoder, **kwargs
    ):
        pytest.fail("retriever.retrieve_evidence should not be called on completed refusal resume")


def _fault_service(container):
    """ChatService whose Model / Judge / Retriever all fault on invocation."""
    return ChatService(
        retriever=_FaultRetriever(),
        dense_encoder=FakeDenseEncoder(),
        sparse_encoder=FakeSparseEncoder(),
        model=_FaultModel(),
        resolve_corpus=lambda cid: _eng_corpus(),
        metadata_store=container.metadata_store,
        judge=_FaultJudge(),
        registry=container.corpus_registry,
    )


def test_completed_no_evidence_resume_zero_model_calls() -> None:
    """A 'completed' no-evidence checkpoint must resume deterministically
    WITHOUT calling Retriever / Judge / Model — the stored terminal state
    is authoritative."""
    container = get_default_container()
    _ingest(container, "d1", "The vacation policy grants 20 days paid leave.")
    mstore = container.metadata_store

    ev = _evidence("e1", "vacation text", "d1")
    cov = SufficiencyResult(
        overall_status="insufficient",
        should_abstain=True,
        fact_coverage=(),
    )
    ck = RunCheckpoint(
        run_id="NE-ZERO",
        tenant_id="t1",
        user_id="u1",
        session_id="s1",
        policy_version="1.0",
        query="q",
        corpus_id="eng",
        max_rounds=5,
        required_facts=_facts("vacation policy"),
        round_index=1,
        evidence=(ev,),
        prior_queries=["q"],
        seen_text_hashes=[ev.text_hash],
        seen_doc_versions=[(ev.document_id, ev.document_version)],
        retrieval_calls=1,
        gap_rounds=1,
        final_reason="no_evidence",
        conflict_stop=False,
        coverage=cov,
        final_report=None,
        final_evidence_ids=[],
        first_result=FastPathResult(
            query="q",
            corpus_id="eng",
            tenant_id="t1",
            evidence=(ev,),
            sufficiency=FastPathSufficiency.SUFFICIENT,
            stop_reason=FastPathStopReason.EVIDENCE_FOUND,
        ),
    )
    # Step 1: save as running (save_run_checkpoint always writes status=running).
    mstore.save_run_checkpoint(ck)
    # Step 2: mark completed so the DB truly shows status=completed.
    mstore.mark_run_checkpoint_done("NE-ZERO")
    assert mstore.load_run_checkpoint("NE-ZERO").status == "completed"

    # Resume with FAULT service — any call to model/judge/retriever fails the test.
    svc = _fault_service(container)
    resumed = svc.resume_run("NE-ZERO", _ctx())
    assert resumed.abstained is True

    # Verify checkpoint is still completed (resume didn't corrupt).
    assert mstore.load_run_checkpoint("NE-ZERO").status == "completed"

    # Second resume — idempotent.
    resumed2 = svc.resume_run("NE-ZERO", _ctx())
    assert resumed2.abstained == resumed.abstained


def test_completed_judge_fault_resume_zero_model_calls() -> None:
    """A 'completed' judge-fault checkpoint must resume deterministically
    WITHOUT calling Retriever / Judge / Model."""
    container = get_default_container()
    _ingest(container, "d1", "The vacation policy grants 20 days paid leave.")
    mstore = container.metadata_store

    ev = _evidence("e1", "vacation text", "d1")
    cov = SufficiencyResult(
        overall_status="insufficient",
        should_abstain=True,
        fact_coverage=(),
    )
    ck = RunCheckpoint(
        run_id="JF-ZERO",
        tenant_id="t1",
        user_id="u1",
        session_id="s1",
        policy_version="1.0",
        query="q",
        corpus_id="eng",
        max_rounds=5,
        required_facts=_facts("vacation policy"),
        round_index=1,
        evidence=(ev,),
        prior_queries=["q"],
        seen_text_hashes=[ev.text_hash],
        seen_doc_versions=[(ev.document_id, ev.document_version)],
        retrieval_calls=1,
        gap_rounds=1,
        final_reason="judge_fault",
        conflict_stop=False,
        coverage=cov,
        final_report=None,
        final_evidence_ids=[],
        first_result=FastPathResult(
            query="q",
            corpus_id="eng",
            tenant_id="t1",
            evidence=(ev,),
            sufficiency=FastPathSufficiency.SUFFICIENT,
            stop_reason=FastPathStopReason.EVIDENCE_FOUND,
        ),
    )
    mstore.save_run_checkpoint(ck)
    mstore.mark_run_checkpoint_done("JF-ZERO")
    assert mstore.load_run_checkpoint("JF-ZERO").status == "completed"

    svc = _fault_service(container)
    resumed = svc.resume_run("JF-ZERO", _ctx())
    assert resumed.abstained is True
    assert mstore.load_run_checkpoint("JF-ZERO").status == "completed"


def test_running_checkpoint_revoke_all_evidence_marks_completed() -> None:
    """A 'running' checkpoint whose ALL evidence is revoked on resume must
    return no-evidence refusal AND persist status='completed'."""
    container = get_default_container()
    _ingest(container, "d1", "The vacation policy grants 20 days paid leave.")
    mstore = container.metadata_store

    ev = _evidence("e1", "The vacation policy grants 20 days paid leave.", "d1")
    ck = RunCheckpoint(
        run_id="REVOKE-ALL",
        tenant_id="t1",
        user_id="u1",
        session_id="s1",
        policy_version="1.0",
        query="q",
        corpus_id="eng",
        max_rounds=5,
        required_facts=_facts("vacation policy"),
        round_index=1,
        evidence=(ev,),
        prior_queries=["q"],
        seen_text_hashes=[ev.text_hash],
        seen_doc_versions=[(ev.document_id, ev.document_version)],
        retrieval_calls=1,
        gap_rounds=1,
        final_reason=None,
        conflict_stop=False,
        coverage=None,
        final_report=None,
        final_evidence_ids=[],
        first_result=FastPathResult(
            query="q",
            corpus_id="eng",
            tenant_id="t1",
            evidence=(ev,),
            sufficiency=FastPathSufficiency.SUFFICIENT,
            stop_reason=FastPathStopReason.EVIDENCE_FOUND,
        ),
    )
    mstore.save_run_checkpoint(ck)
    assert mstore.load_run_checkpoint("REVOKE-ALL").status == "running"

    # Tighten d1's ACL so u1 can no longer read it → all evidence revoked.
    mstore.update_document_acl(
        "t1",
        "eng",
        "d1",
        "v1",
        security_level="public",
        acl_scope="restricted",
        allowed_user_ids=["other"],
        allowed_group_ids=[],
        denied_user_ids=[],
        denied_group_ids=[],
    )

    # Use fault service: the all-revoked path returns refusal without model.
    svc = _fault_service(container)
    resumed = svc.resume_run("REVOKE-ALL", _ctx())
    assert resumed.abstained is True

    # The checkpoint must now be 'completed' (not left as 'running').
    assert mstore.load_run_checkpoint("REVOKE-ALL").status == "completed"


def test_completed_checkpoint_flips_to_running_on_evidence_revocation() -> None:
    """When a completed checkpoint loses evidence (ACL revoke), resume must
    flip status to 'running', recompute derived state, and eventually mark
    'completed' again — returning a valid answer from surviving evidence."""
    container = get_default_container()
    _ingest(container, "d1", "The vacation policy grants 20 days paid leave.")
    _ingest(container, "d2", "The bonus policy grants a 10% bonus.")
    mstore = container.metadata_store

    ev1 = _evidence("e1", "Vacation policy text", "d1")
    ev2 = _evidence("e2", "Bonus policy text", "d2")
    cov = SufficiencyResult(
        overall_status="sufficient",
        should_abstain=False,
        fact_coverage=(),
    )
    ck = RunCheckpoint(
        run_id="FLIP-RUN",
        tenant_id="t1",
        user_id="u1",
        session_id="s1",
        policy_version="1.0",
        query="q",
        corpus_id="eng",
        max_rounds=5,
        required_facts=_facts("vacation policy", "bonus structure"),
        round_index=2,
        evidence=(ev1, ev2),
        prior_queries=["q", "bonus structure"],
        seen_text_hashes=[ev1.text_hash, ev2.text_hash],
        seen_doc_versions=[
            (ev1.document_id, ev1.document_version),
            (ev2.document_id, ev2.document_version),
        ],
        retrieval_calls=2,
        gap_rounds=2,
        final_reason="sufficient",
        conflict_stop=False,
        coverage=cov,
        final_report=None,
        final_evidence_ids=["e1", "e2"],
        first_result=FastPathResult(
            query="q",
            corpus_id="eng",
            tenant_id="t1",
            evidence=(ev1,),
            sufficiency=FastPathSufficiency.SUFFICIENT,
            stop_reason=FastPathStopReason.EVIDENCE_FOUND,
        ),
    )
    mstore.save_run_checkpoint(ck)
    mstore.mark_run_checkpoint_done("FLIP-RUN")
    assert mstore.load_run_checkpoint("FLIP-RUN").status == "completed"

    # Revoke d2 — only e1 survives re-auth.
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

    svc = _service(container, _FakeRetriever({"q": [ev1], "bonus structure": []}))
    resumed = svc.resume_run("FLIP-RUN", _ctx())
    # Surviving evidence is only e1 (e2 was revoked).
    assert {e.evidence_id for e in resumed.evidence} == {"e1"}
    # Final status is completed (the loop+mark_done re-completed it).
    assert mstore.load_run_checkpoint("FLIP-RUN").status == "completed"
