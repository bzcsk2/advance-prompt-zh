"""E-021 integration: temporal filter + conflict resolver wired into ChatService.

Hermetic (fake retriever + fake model, no Qdrant/LLM). Asserts:
* single-corpus ``answer`` surfaces a CONTRADICTED conflict as
  ``completeness == "conflicted"`` and instructs the model to present both
  sources (issue #2 — the resolver never judges sufficiency itself);
* multi-corpus ``answer_multi_corpus`` behaves identically on merged evidence;
* the no-conflict path keeps its existing M2–M5 behaviour (``completeness``
  unchanged, ``conflict_report`` is ``NONE``) — regression gate.
"""

from datetime import datetime

from agentic_rag_enterprise.corpus.registry import InMemoryCorpusRegistry
from agentic_rag_enterprise.services.claims_schema import ClaimExtraction
from tests.fixtures import FakeDenseEncoder, FakeSparseEncoder
from agentic_rag_enterprise.domain.corpus import CorpusConfig
from agentic_rag_enterprise.domain.evidence import Evidence as SnapshotEvidence
from agentic_rag_enterprise.domain.security import SecurityContext
from agentic_rag_enterprise.judge.models import SufficiencyResult
from agentic_rag_enterprise.retrieval.models import CorpusNotDiscoverableError
from agentic_rag_enterprise.services.chat_service import ChatService


# --- shared fakes ------------------------------------------------------------


def _evidence(
    evid,
    *,
    doc="d1",
    ver="v1",
    auth=50,
    text="x",
    corpus_id="eng",
    tenant_id="t1",
    effective_from=None,
    effective_to=None,
):
    return SnapshotEvidence(
        evidence_id=evid,
        tenant_id=tenant_id,
        corpus_id=corpus_id,
        document_id=doc,
        document_version=ver,
        source_uri=f"inline://{doc}",
        source_filename=f"{doc}.md",
        text=text,
        text_hash=f"h-{evid}",
        retrieval_query="q",
        authority_level=auth,
        retrieved_at=datetime(2024, 1, 1),
        effective_from=effective_from,
        effective_to=effective_to,
        acl_policy_id="default",
        policy_version="1.0",
        retrieval_iteration=0,
    )


def _ctx(tenant_id="t1"):
    return SecurityContext(
        request_id="r",
        session_id="s",
        tenant_id=tenant_id,
        user_id="u1",
        policy_version="1.0",
    )


def _corpus(corpus_id="eng", tenant_id="t1", authority_level=50):
    return CorpusConfig(
        corpus_id=corpus_id,
        tenant_id=tenant_id,
        name=corpus_id,
        description="",
        domain="",
        owner="",
        source_type="documents",
        capability_ids=[],
        enabled=True,
        searchable=True,
        authority_level=authority_level,
        security_policy_id="p",
        default_security_level="internal",
        created_at=datetime(2024, 1, 1),
        updated_at=datetime(2024, 1, 1),
    )


class _SingleRetriever:
    """Returns one fixed payload for the single-corpus Fast Path."""

    def __init__(self, payload):
        self._payload = payload

    def retrieve_evidence(
        self,
        ctx,
        query,
        corpus,
        top_k=None,
        *,
        dense_encoder,
        sparse_encoder,
        iteration=0,
        plan_step_id=None,
    ):
        if corpus.tenant_id != ctx.tenant_id:
            raise CorpusNotDiscoverableError(f"{corpus.corpus_id} not discoverable")
        return list(self._payload)


class _MultiRetriever:
    """Per-corpus payload for the multi-corpus merge path."""

    def __init__(self, per_corpus):
        self._per_corpus = per_corpus

    def retrieve_evidence(
        self,
        ctx,
        query,
        corpus,
        top_k=None,
        *,
        dense_encoder,
        sparse_encoder,
        iteration=0,
        plan_step_id=None,
    ):
        return list(self._per_corpus.get(corpus.corpus_id, []))


class _RecordingModel:
    """Fake model that records the last synthesis prompt."""

    def __init__(self, extraction):
        self._extraction = extraction
        self.last_messages = None

    def invoke(self, messages, **kwargs):
        return ""

    def with_structured_output(self, schema, **kwargs):
        return self._Wrap(self)

    class _Wrap:
        def __init__(self, model):
            self._model = model

        def invoke(self, messages, **kwargs):
            self._model.last_messages = messages
            return self._model._extraction


def _single_service(payload):
    return ChatService(
        retriever=_SingleRetriever(payload),
        dense_encoder=FakeDenseEncoder(),
        sparse_encoder=FakeSparseEncoder(),
        model=_RecordingModel(ClaimExtraction(draft_answer="draft", claims=[])),
        resolve_corpus=lambda cid: _corpus(corpus_id=cid),
    )


def _multi_service(per_corpus):
    registry = InMemoryCorpusRegistry(corpora=[_corpus(corpus_id=cid) for cid in per_corpus])
    return ChatService(
        retriever=_MultiRetriever(per_corpus),
        dense_encoder=FakeDenseEncoder(),
        sparse_encoder=FakeSparseEncoder(),
        model=_RecordingModel(ClaimExtraction(draft_answer="draft", claims=[])),
        resolve_corpus=lambda cid: _corpus(corpus_id=cid),
        registry=registry,
    )


class _RecordingJudge:
    """Deterministic judge that records the evidence it was asked to cover (P1-2)."""

    name = "recording"

    def __init__(self):
        self.calls = 0
        self.last_evidence: tuple = ()

    def judge(self, *, query, required_facts, evidence, timeout=None):
        self.calls += 1
        self.last_evidence = evidence
        return SufficiencyResult(
            overall_status="sufficient",
            should_abstain=False,
            fact_coverage=(),
            covered_fact_ids=(),
            can_continue_retrieval=False,
        )


class _CountingSingleRetriever(_SingleRetriever):
    """Single-corpus retriever that counts every ``retrieve_evidence`` call."""

    def __init__(self, payload):
        super().__init__(payload)
        self.calls = 0

    def retrieve_evidence(
        self,
        ctx,
        query,
        corpus,
        top_k=None,
        *,
        dense_encoder,
        sparse_encoder,
        iteration=0,
        plan_step_id=None,
    ):
        self.calls += 1
        return super().retrieve_evidence(
            ctx,
            query,
            corpus,
            top_k,
            dense_encoder=dense_encoder,
            sparse_encoder=sparse_encoder,
            iteration=iteration,
        )


def _single_service_with(payload, *, judge=None):
    return ChatService(
        retriever=_CountingSingleRetriever(payload),
        dense_encoder=FakeDenseEncoder(),
        sparse_encoder=FakeSparseEncoder(),
        model=_RecordingModel(ClaimExtraction(draft_answer="draft", claims=[])),
        resolve_corpus=lambda cid: _corpus(corpus_id=cid),
    )


# --- single-corpus: unresolvable conflict ------------------------------------


def test_single_corpus_contradicted_conflict_is_surfaced() -> None:
    ev_a = _evidence("ea", doc="da", auth=50, text="version: 2")
    ev_b = _evidence("eb", doc="db", auth=50, text="version: 3")
    svc = _single_service([ev_a, ev_b])
    env = svc.answer("当前 API 版本是多少", _ctx(), "eng")

    assert env.abstained is False
    assert env.completeness == "conflicted"
    assert env.conflict_report is not None
    assert env.conflict_report.conflict_status.value == "contradicted"
    # Both conflicting sources are preserved for synthesis.
    assert set(env.conflict_report.resolved_evidence_ids) == {"ea", "eb"}
    # The model is told to present BOTH sources, not pick one.
    prompt = "\n".join(m["content"] for m in svc._model.last_messages)
    assert "CONFLICT DETECTED" in prompt
    assert "ea" in prompt and "eb" in prompt


def test_single_corpus_no_conflict_keeps_existing_behaviour() -> None:
    ev = _evidence("o", doc="d1", auth=50, text="a free-text migration note")
    svc = _single_service([ev])
    env = svc.answer("如何配置超时", _ctx(), "eng")

    assert env.conflict_report is not None
    assert env.conflict_report.conflict_status.value == "none"
    # Existing M2–M5 behaviour is preserved: no conflict signal → the resolver's
    # verdict is NONE and the envelope completeness is never escalated to
    # "conflicted" (it stays at its normal sufficient value).
    assert env.completeness != "conflicted"
    assert env.completeness in ("complete", "partial")


def test_single_corpus_authority_conflict_resolves_silently() -> None:
    prod = _evidence("pd", doc="prod", auth=80, text="version: 2")
    ticket = _evidence("tk", doc="ticket", auth=40, text="version: 3")
    svc = _single_service([prod, ticket])
    env = svc.answer("当前 API 版本", _ctx(), "eng")

    # Resolved (not contradicted) → completeness stays the normal sufficient
    # value (not escalated to "conflicted").
    assert env.conflict_report.conflict_status.value == "resolved"
    assert env.completeness != "conflicted"
    assert env.conflict_report.resolved_evidence_ids == ("pd",)


# --- P1-1: temporal filter drops ALL evidence → conservative refusal ----------


def test_single_corpus_all_expired_abstains_without_model() -> None:
    expired_a = _evidence(
        "ea", doc="da", auth=50, text="version: 2", effective_to=datetime(2020, 1, 1)
    )
    expired_b = _evidence(
        "eb", doc="db", auth=50, text="version: 3", effective_to=datetime(2020, 1, 1)
    )
    svc = _single_service_with([expired_a, expired_b])
    env = svc.answer("当前 API 版本是多少", _ctx(), "eng")

    # Everything expired → temporal filter empties evidence → refuse without
    # ever invoking the model (P1-1).
    assert env.abstained is True
    assert env.completeness == "insufficient"
    assert env.stop_reason == "no_evidence"
    assert svc._model.last_messages is None


def test_multi_corpus_all_out_of_window_abstains_without_model() -> None:
    ev_a = _evidence(
        "ea",
        doc="da",
        auth=50,
        text="version: 2",
        corpus_id="product_docs",
        effective_to=datetime(2020, 1, 1),
    )
    ev_b = _evidence(
        "eb",
        doc="db",
        auth=50,
        text="version: 3",
        corpus_id="tickets",
        effective_to=datetime(2020, 1, 1),
    )
    svc = _multi_service({"product_docs": [ev_a], "tickets": [ev_b]})
    env = svc.answer_multi_corpus("当前 API 版本", _ctx(), corpus_ids=["product_docs", "tickets"])

    assert env.abstained is True
    assert env.completeness == "insufficient"
    assert env.stop_reason == "no_evidence"
    assert svc._model.last_messages is None


# --- P1-2: iteration runs the conflict stage before the Judge each round ------


def test_iteration_as_of_judge_sees_only_in_window_evidence() -> None:
    # Query scoped "as of 2020-01-01". Only ev_a is effective then; ev_b becomes
    # effective later and must be excluded from the Judge's view.
    in_window = _evidence(
        "ea",
        doc="da",
        auth=50,
        text="version: 2",
        effective_from=datetime(2019, 1, 1),
        effective_to=datetime(2021, 1, 1),
    )
    future = _evidence(
        "eb",
        doc="db",
        auth=50,
        text="version: 3",
        effective_from=datetime(2022, 1, 1),
    )
    judge = _RecordingJudge()
    svc = _single_service_with([in_window, future])
    svc.answer_with_iteration(
        "截至2020-01-01 当前 API 版本是多少", _ctx(), "eng", max_rounds=3, judge=judge
    )

    # The Judge must only ever see the in-window evidence (P1-2): ev_b is dropped
    # by the temporal filter before the Judge runs.
    assert judge.last_evidence is not None
    seen_ids = {ev.evidence_id for ev in judge.last_evidence}
    assert seen_ids == {"ea"}
    assert "eb" not in seen_ids


def test_iteration_contradicted_stops_before_gap_retrieval() -> None:
    ev_a = _evidence("ea", doc="da", auth=50, text="version: 2")
    ev_b = _evidence("eb", doc="db", auth=50, text="version: 3")
    retriever = _CountingSingleRetriever([ev_a, ev_b])
    judge = _RecordingJudge()
    svc = ChatService(
        retriever=retriever,
        dense_encoder=FakeDenseEncoder(),
        sparse_encoder=FakeSparseEncoder(),
        model=_RecordingModel(ClaimExtraction(draft_answer="draft", claims=[])),
        resolve_corpus=lambda cid: _corpus(corpus_id=cid),
    )
    env = svc.answer_with_iteration("当前 API 版本是多少", _ctx(), "eng", max_rounds=3, judge=judge)

    # Round 0 already contradicts → the loop breaks and no gap retrieval runs.
    assert env.completeness == "conflicted"
    assert env.conflict_report.conflict_status.value == "contradicted"
    # run_fast_path calls retrieve_evidence once; gap retrieval must never run.
    assert retriever.calls == 1


# --- P1-3: CONTRADICTED answer is deterministic, model-claim-independent ------


def test_contradicted_answer_lists_both_sources_with_times_without_model_claims() -> None:
    # Both sources are effective "now" (permanent) so the current-mode filter
    # keeps them and the resolver reaches the contradiction; they carry distinct
    # effective_from windows so the deterministic renderer has times to show.
    ev_a = _evidence(
        "ea",
        doc="da",
        auth=50,
        text="version: 2",
        effective_from=datetime(2020, 1, 1),
    )
    ev_b = _evidence(
        "eb",
        doc="db",
        auth=50,
        text="version: 3",
        effective_from=datetime(2022, 1, 1),
    )
    svc = _single_service([ev_a, ev_b])
    env = svc.answer("当前 API 版本是多少", _ctx(), "eng")

    assert env.completeness == "conflicted"
    # Even though the model returned NO claims, the deterministic renderer must
    # still list both conflicting sources and their effective windows (P1-3), and
    # must NOT fall back to the generic "no supported claim" text.
    assert "ea" in env.answer_markdown and "eb" in env.answer_markdown
    assert "2020-01-01" in env.answer_markdown
    assert "2022-01-01" in env.answer_markdown
    assert "No supported claim could be established" not in env.answer_markdown
    assert env.claims == ()


# --- multi-corpus: unresolvable conflict --------------------------------------


def test_multi_corpus_contradicted_conflict_is_surfaced() -> None:
    ev_a = _evidence("ea", doc="da", auth=50, text="version: 2", corpus_id="product_docs")
    ev_b = _evidence("eb", doc="db", auth=50, text="version: 3", corpus_id="tickets")
    svc = _multi_service({"product_docs": [ev_a], "tickets": [ev_b]})
    env = svc.answer_multi_corpus("当前 API 版本", _ctx(), corpus_ids=["product_docs", "tickets"])

    assert env.completeness == "conflicted"
    assert env.conflict_report is not None
    assert env.conflict_report.conflict_status.value == "contradicted"
    assert set(env.conflict_report.resolved_evidence_ids) == {"ea", "eb"}
