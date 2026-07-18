"""Unit tests for E-019/E-020 ChatService.answer_with_iteration.

Hermetic: a query-keyed fake ``SecureRetriever`` and a fake model that emits one
claim per ``[evidence_id]`` marker. Asserts:

* ``answer()`` stays single-pass (E-014 green) and attaches no coverage;
* ``answer_with_iteration`` with a judge attaches coverage and maps the verdict to
  completeness (sufficient→complete, partially_sufficient→partial, contradicted→conflicted);
* an insufficient coverage verdict abstains with coverage attached;
* the bounded loop honours ``max_rounds`` and stops early on ``no_new_evidence``;
* ``FastPathBackendError`` propagates; a ``JudgeTimeoutError`` degrades
  conservatively (abstain), never a fabricated complete answer.
"""

from __future__ import annotations

import re
from datetime import datetime

import pytest

from agentic_rag_enterprise.answer.envelope import Claim, TenantBindingError
from agentic_rag_enterprise.domain.corpus import CorpusConfig
from agentic_rag_enterprise.domain.evidence import Evidence as SnapshotEvidence
from agentic_rag_enterprise.domain.security import SecurityContext
from agentic_rag_enterprise.judge.deterministic_coverage_judge import (
    DeterministicCoverageJudge,
)
from agentic_rag_enterprise.judge.models import RequiredFact
from agentic_rag_enterprise.judge.protocol import JudgeTimeoutError
from agentic_rag_enterprise.judge.query_fact_extractor import make_required_fact
from agentic_rag_enterprise.retrieval.fast_path import FastPathBackendError
from agentic_rag_enterprise.retrieval.models import CorpusNotDiscoverableError
from agentic_rag_enterprise.services.chat_service import ChatService
from agentic_rag_enterprise.services.claims_schema import ClaimExtraction
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


def _evidence(
    evidence_id: str,
    text: str,
    tenant_id: str = "t1",
    corpus_id: str = "eng",
    document_version: str = "v1",
    text_hash: str | None = None,
) -> SnapshotEvidence:
    return SnapshotEvidence(
        evidence_id=evidence_id,
        tenant_id=tenant_id,
        corpus_id=corpus_id,
        document_id="d1",
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


def _ctx(tenant_id: str = "t1") -> SecurityContext:
    return SecurityContext(
        request_id="r1",
        session_id="s1",
        tenant_id=tenant_id,
        user_id="u1",
        policy_version="1.0",
    )


class _FakeLoopRetriever:
    """Query-keyed fake retriever that records every retrieve_evidence call.

    A map value may be an ``Exception`` instance to simulate a retrieval fault
    (which ``run_fast_path`` wraps as ``FastPathBackendError``).
    """

    def __init__(self, evidence_map: dict[str, object]) -> None:
        self._map = evidence_map
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
        payload = self._map.get(query, [])
        if isinstance(payload, Exception):
            raise payload
        return list(payload)


class _LoopModel:
    """Fake model: one claim per ``[evidence_id]`` marker in the prompt."""

    def __init__(self) -> None:
        self.last_messages: list[dict[str, str]] | None = None

    def invoke(self, messages: list[dict[str, str]], **kwargs: object) -> str:
        return ""

    def with_structured_output(self, schema: type, **kwargs: object) -> object:
        return self._Wrapper(self, schema)

    class _Wrapper:
        def __init__(self, model: "_LoopModel", schema: type) -> None:
            self._model = model
            self._schema = schema

        def invoke(self, messages: list[dict[str, str]], **kwargs: object):
            self._model.last_messages = messages
            claims = _claims_from_messages(messages)
            return ClaimExtraction(draft_answer="\n".join(c.text for c in claims), claims=claims)


def _claims_from_messages(messages: list[dict[str, str]]) -> list[Claim]:
    """Emit one ``Claim`` per evidence id whose text is the cited evidence text.

    Mirrors the eval runner: the claim text overlaps the evidence so it survives
    the Stage-B verifier (the old ``finding from <id>`` text never overlapped and
    would have forced every answer to ``partial``, masking the Stage-B downgrade).
    """
    blob = "\n".join(m.get("content", "") for m in messages)
    claims: list[Claim] = []
    for segment in re.split(r"(?=\[[A-Za-z0-9_-]+\])", blob):
        match = re.match(r"\[([A-Za-z0-9_-]+)\]\s*[^\n]*\n(.*?)(?:\n\n|\Z)", segment, re.DOTALL)
        if not match:
            continue
        evidence_id = match.group(1)
        text = match.group(2).strip()
        if not text:
            continue
        claims.append(Claim(claim_id=f"c{len(claims)}", text=text, evidence_ids=(evidence_id,)))
    return claims


def _service(retriever: _FakeLoopRetriever, model: _LoopModel | None = None) -> ChatService:
    return ChatService(
        retriever=retriever,
        dense_encoder=FakeDenseEncoder(),
        sparse_encoder=FakeSparseEncoder(),
        model=model or _LoopModel(),
        resolve_corpus=lambda cid: _corpus(corpus_id=cid, tenant_id="t1"),
    )


def _facts(*descs: str) -> list[RequiredFact]:
    return [make_required_fact(d) for d in descs]


# --- E-014 single-pass stays green ----------------------------------------


def test_answer_is_single_pass_without_coverage() -> None:
    retriever = _FakeLoopRetriever(
        {"q": [_evidence("e1", "The vacation policy grants 20 days paid leave.")]}
    )
    env = _service(retriever).answer("q", _ctx(), "eng")
    assert env.abstained is False
    assert env.coverage is None
    assert env.gap_rounds == 1
    assert env.completeness == "complete"
    assert len(retriever.calls) == 1  # exactly one retrieve_evidence


def test_answer_single_pass_insufficient_abstains() -> None:
    retriever = _FakeLoopRetriever({})
    env = _service(retriever).answer("q", _ctx(), "eng")
    assert env.abstained is True
    assert env.coverage is None
    assert env.stop_reason == "no_evidence"


# --- E-019 single-pass judge attaches coverage ---------------------------


def test_single_pass_judge_attaches_sufficient_coverage() -> None:
    retriever = _FakeLoopRetriever(
        {"q": [_evidence("e1", "The vacation policy grants 20 days paid leave.")]}
    )
    env = _service(retriever).answer_with_iteration(
        "q",
        _ctx(),
        "eng",
        max_rounds=1,
        judge=DeterministicCoverageJudge(),
        required_facts=_facts("vacation policy"),
    )
    assert env.coverage is not None
    assert env.coverage.overall_status == "sufficient"
    assert env.completeness == "complete"
    assert env.gap_rounds == 1


def test_single_pass_judge_partial_maps_to_partial_with_missing() -> None:
    retriever = _FakeLoopRetriever(
        {"q": [_evidence("e1", "The vacation policy grants 20 days paid leave.")]}
    )
    env = _service(retriever).answer_with_iteration(
        "q",
        _ctx(),
        "eng",
        max_rounds=1,
        judge=DeterministicCoverageJudge(),
        required_facts=_facts("vacation policy", "bonus structure"),
    )
    assert env.coverage.overall_status == "partially_sufficient"
    assert env.completeness == "partial"
    assert env.missing_aspects  # missing fact surfaced


def test_single_pass_judge_contradicted_maps_to_conflicted() -> None:
    retriever = _FakeLoopRetriever(
        {"q": [_evidence("e1", "The office is not in new york; it is in boston.")]}
    )
    env = _service(retriever).answer_with_iteration(
        "q",
        _ctx(),
        "eng",
        max_rounds=1,
        judge=DeterministicCoverageJudge(),
        required_facts=_facts("office in new york"),
    )
    assert env.coverage.overall_status == "contradicted"
    assert env.completeness == "conflicted"
    assert env.abstained is False


# --- E-020 insufficient coverage abstains (lock preserved) ---------------


def test_insufficient_coverage_abstains_with_coverage() -> None:
    retriever = _FakeLoopRetriever({"q": [_evidence("e1", "the weather is sunny today")]})
    env = _service(retriever).answer_with_iteration(
        "q",
        _ctx(),
        "eng",
        max_rounds=3,
        judge=DeterministicCoverageJudge(),
        required_facts=_facts("secret project codename"),
    )
    assert env.abstained is True
    assert env.completeness == "insufficient"
    assert env.stop_reason == "no_evidence"
    assert env.coverage is not None
    assert env.coverage.overall_status == "insufficient"


# --- E-020 bounded loop ---------------------------------------------------


def test_max_rounds_honoured() -> None:
    # alpha covered; gamma never covered (gap returns unrelated new evidence).
    retriever = _FakeLoopRetriever(
        {
            "q": [_evidence("e1", "The alpha requirement is met.")],
            "gamma specification": [
                _evidence("e2", "unrelated sigma information."),
            ],
        }
    )
    env = _service(retriever).answer_with_iteration(
        "q",
        _ctx(),
        "eng",
        max_rounds=2,
        judge=DeterministicCoverageJudge(),
        required_facts=_facts("alpha requirement", "gamma specification"),
    )
    assert env.gap_rounds == 2  # did not exceed max_rounds
    assert len(retriever.calls) == 2
    assert env.completeness == "partial"


def test_all_sources_exhausted_when_no_gap_queries() -> None:
    # The gap query returns nothing new, and GapPlanner has no further candidate
    # after it (the query is added to `prior_queries`). The loop must stop
    # immediately with `all_sources_exhausted` rather than spinning an empty
    # re-judge round (P1-A). Round 0 gains; round 1 runs the single gap query and
    # gains nothing; round 2 has no candidate query -> stop.
    retriever = _FakeLoopRetriever(
        {
            "q": [_evidence("e1", "The alpha requirement is met.")],
            "gamma specification": [],  # gap returns nothing new
        }
    )
    env = _service(retriever).answer_with_iteration(
        "q",
        _ctx(),
        "eng",
        max_rounds=5,
        judge=DeterministicCoverageJudge(),
        required_facts=_facts("alpha requirement", "gamma specification"),
    )
    assert env.gap_rounds == 3
    assert len(retriever.calls) == 2
    assert env.completeness == "partial"
    assert env.stop_reason == "all_sources_exhausted"


def test_no_new_evidence_is_unreachable_when_no_candidate_queries() -> None:
    # A fresh id carrying the SAME text hash as already-seen evidence must NOT be
    # counted as a gain (P2-2): the loop reaches `all_sources_exhausted`, not a
    # fabricated `no_new_evidence` after an empty spin. The StopPolicy-level
    # `no_new_evidence` branch (two consecutive genuinely gainless rounds) is
    # covered directly in tests/unit/judge/test_stop_policy.py.
    retriever = _FakeLoopRetriever(
        {
            "q": [_evidence("e1", "The alpha requirement is met.")],
            "gamma specification": [
                _evidence("e2", "The alpha requirement is met.", text_hash="h-e1")
            ],
        }
    )
    env = _service(retriever).answer_with_iteration(
        "q",
        _ctx(),
        "eng",
        max_rounds=6,
        judge=DeterministicCoverageJudge(),
        required_facts=_facts("alpha requirement", "gamma specification"),
    )
    assert env.stop_reason == "all_sources_exhausted"
    assert env.completeness == "partial"


def test_gap_retrieval_rejects_cross_tenant_evidence() -> None:
    # Round 0 returns tenant-t1 evidence; a gap query returns tenant-t2 evidence.
    # The accumulated gap snapshot must never enter the answer (P1-1): fail closed
    # with TenantBindingError before any model prompt / envelope.
    retriever = _FakeLoopRetriever(
        {
            "q": [_evidence("e1", "The alpha requirement is met.", tenant_id="t1")],
            "gamma specification": [_evidence("e2", "gamma detail", tenant_id="t2")],
        }
    )
    with pytest.raises(TenantBindingError):
        _service(retriever).answer_with_iteration(
            "q",
            _ctx(tenant_id="t1"),
            "eng",
            max_rounds=3,
            judge=DeterministicCoverageJudge(),
            required_facts=_facts("alpha requirement", "gamma specification"),
        )


def test_gap_retrieval_rejects_cross_corpus_evidence() -> None:
    # A gap snapshot belonging to a different corpus must be rejected (P1-1).
    retriever = _FakeLoopRetriever(
        {
            "q": [_evidence("e1", "The alpha requirement is met.", corpus_id="eng")],
            "gamma specification": [_evidence("e2", "gamma detail", corpus_id="other")],
        }
    )
    with pytest.raises(TenantBindingError):
        _service(retriever).answer_with_iteration(
            "q",
            _ctx(),
            "eng",
            max_rounds=3,
            judge=DeterministicCoverageJudge(),
            required_facts=_facts("alpha requirement", "gamma specification"),
        )


def test_max_rounds_reason_recorded() -> None:
    # The real termination reason must be surfaced on the non-abstain envelope
    # (P2-1), not the always-"evidence_found" Fast Path reason.
    retriever = _FakeLoopRetriever(
        {
            "q": [_evidence("e1", "The alpha requirement is met.")],
            "gamma specification": [_evidence("e2", "unrelated sigma information.")],
        }
    )
    env = _service(retriever).answer_with_iteration(
        "q",
        _ctx(),
        "eng",
        max_rounds=2,
        judge=DeterministicCoverageJudge(),
        required_facts=_facts("alpha requirement", "gamma specification"),
    )
    assert env.abstained is False
    assert env.stop_reason == "max_rounds"


def test_new_document_version_counts_as_gain() -> None:
    # Same evidence id returned in the gap round but with a NEW document version
    # must be treated as new content (§14.6) and accumulated into the answer,
    # proving id-only novelty is not used (P2-2).
    retriever = _FakeLoopRetriever(
        {
            "q": [_evidence("e1", "The alpha requirement is met.", document_version="v1")],
            "gamma specification": [
                _evidence("e1", "The alpha requirement is met, revised.", document_version="v2")
            ],
        }
    )
    env = _service(retriever).answer_with_iteration(
        "q",
        _ctx(),
        "eng",
        max_rounds=4,
        judge=DeterministicCoverageJudge(),
        required_facts=_facts("alpha requirement", "gamma specification"),
    )
    assert any(e.document_version == "v2" for e in env.evidence)
    assert env.gap_rounds >= 2


def test_gap_retrieval_completes_the_answer() -> None:
    retriever = _FakeLoopRetriever(
        {
            "q": [_evidence("e1", "The vacation policy grants 20 days paid leave.")],
            "request time off": [
                _evidence("e2", "Employees request time off via the HR portal."),
            ],
        }
    )
    env = _service(retriever).answer_with_iteration(
        "q",
        _ctx(),
        "eng",
        max_rounds=3,
        judge=DeterministicCoverageJudge(),
        required_facts=_facts("vacation policy", "request time off"),
    )
    assert env.coverage.overall_status == "sufficient"
    assert env.completeness == "complete"
    assert env.gap_rounds == 2
    assert len(retriever.calls) == 2


# --- Fault handling -------------------------------------------------------


def test_fast_path_backend_error_propagates() -> None:
    # A retrieval fault must propagate as FastPathBackendError, never relabelled.
    retriever = _FakeLoopRetriever({"q": RuntimeError("qdrant down")})
    with pytest.raises(FastPathBackendError):
        _service(retriever).answer_with_iteration(
            "q",
            _ctx(),
            "eng",
            max_rounds=3,
            judge=DeterministicCoverageJudge(),
            required_facts=_facts("x"),
        )


def test_judge_timeout_degrades_conservatively() -> None:
    class _TimeoutJudge(DeterministicCoverageJudge):
        def judge(self, **kwargs):  # type: ignore[override]
            raise JudgeTimeoutError("judge timed out")

    retriever = _FakeLoopRetriever(
        {"q": [_evidence("e1", "The vacation policy grants 20 days paid leave.")]}
    )
    env = _service(retriever).answer_with_iteration(
        "q",
        _ctx(),
        "eng",
        max_rounds=3,
        judge=_TimeoutJudge(),
        required_facts=_facts("vacation policy"),
    )
    # Must NOT be a fabricated complete answer; degrades to an abstain.
    assert env.abstained is True
    assert env.completeness == "insufficient"
    assert env.stop_reason == "no_evidence"


# --- Stage B critical/empty claim downgrade (P1-2 acceptance) ----------------


class _StageBModel:
    """Fake model that emits one SUPPORTED claim plus one critical claim whose
    text does NOT lexically overlap any evidence (so Stage B must mark it
    unsupported). The critical claim is asserted critical so the downgrade is
    observable on an otherwise ``sufficient`` coverage.
    """

    def __init__(self, evidence_id: str) -> None:
        self._evidence_id = evidence_id

    def invoke(self, messages: list[dict[str, str]], **kwargs: object) -> str:
        return ""

    def with_structured_output(self, schema: type, **kwargs: object) -> object:
        return self._Wrapper(self, schema)

    class _Wrapper:
        def __init__(self, model: "_StageBModel", schema: type) -> None:
            self._model = model
            self._schema = schema

        def invoke(self, messages: list[dict[str, str]], **kwargs: object):
            eid = self._model._evidence_id
            claims = [
                Claim(
                    claim_id="c_supported",
                    text="The vacation policy grants 20 days paid leave.",
                    importance="supporting",
                    evidence_ids=(eid,),
                ),
                Claim(
                    claim_id="c_critical",
                    text="The bonus pool is funded entirely by crypto holdings.",
                    importance="critical",
                    evidence_ids=(eid,),
                ),
            ]
            return ClaimExtraction(draft_answer="\n".join(c.text for c in claims), claims=claims)


def test_stage_b_downgrades_sufficient_when_critical_claim_unsupported() -> None:
    # P1-2 acceptance: a `sufficient` Stage-A coverage must NOT reach a `complete`
    # envelope when Stage B removes a CRITICAL claim (the critical claim's text
    # does not overlap the evidence). The builder forces the envelope down to
    # partial/low (never a confident complete answer).
    retriever = _FakeLoopRetriever(
        {"q": [_evidence("e1", "The vacation policy grants 20 days paid leave.")]}
    )
    model = _StageBModel("e1")
    service = ChatService(
        retriever=retriever,
        dense_encoder=FakeDenseEncoder(),
        sparse_encoder=FakeSparseEncoder(),
        model=model,
        resolve_corpus=lambda cid: _corpus(corpus_id=cid, tenant_id="t1"),
    )
    env = service.answer_with_iteration(
        "q",
        _ctx(),
        "eng",
        max_rounds=1,
        judge=DeterministicCoverageJudge(),
        required_facts=_facts("vacation policy"),
    )
    # Coverage says sufficient, but Stage B removed the critical claim.
    assert env.coverage is not None
    assert env.coverage.overall_status == "sufficient"
    assert env.completeness == "partial"
    assert env.confidence == "low"
    # The critical (unsupported) claim must NOT appear in the final answer.
    assert all(c.claim_id != "c_critical" for c in env.claims)
