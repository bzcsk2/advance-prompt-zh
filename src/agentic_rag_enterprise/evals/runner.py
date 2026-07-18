"""E-020 eval runner (build plan §14 / M3).

Drives ``ChatService.answer_with_iteration`` through the deterministic
``DeterministicCoverageJudge`` using a hermetic fake retriever + fake model, so
the loop, gap retrieval, and coverage verdicts can be asserted offline. No LLM
and no network are touched.

The fake model parses the ``[evidence_id]`` markers the synthesis prompt emits
and returns one atomic ``Claim`` per cited evidence id, so the answer envelope is
always well-formed and the Stage B verifier has real claims to check.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from qdrant_client.models import SparseVector

from agentic_rag_enterprise.answer.envelope import AnswerEnvelope, Claim
from agentic_rag_enterprise.domain.corpus import CorpusConfig
from agentic_rag_enterprise.domain.evidence import Evidence as SnapshotEvidence
from agentic_rag_enterprise.domain.security import SecurityContext
from agentic_rag_enterprise.evals.dataset import EvalCase
from agentic_rag_enterprise.judge.deterministic_coverage_judge import (
    DeterministicCoverageJudge,
)
from agentic_rag_enterprise.judge.models import RequiredFact
from agentic_rag_enterprise.judge.query_fact_extractor import make_required_fact
from agentic_rag_enterprise.judge.protocol import Judge
from agentic_rag_enterprise.providers import ModelProfile
from agentic_rag_enterprise.services.chat_service import ChatService
from agentic_rag_enterprise.services.claims_schema import ClaimExtraction
from agentic_rag_enterprise.storage.vector_store import DenseEncoder, SparseEncoder


class _DummyDenseEncoder(DenseEncoder):
    def __call__(self, text: str) -> list[float]:
        return [0.0]


class _DummySparseEncoder(SparseEncoder):
    def __call__(self, text: str) -> SparseVector:
        return SparseVector(indices=[], values=[])


class _EvalRetriever:
    """Fake ``SecureRetriever`` surface: returns dataset evidence per query.

    Only the ``retrieve_evidence`` surface used by the service loop is
    implemented; the secure-discoverability / parent-auth machinery is irrelevant
    for an offline eval (the dataset is pre-authorized).
    """

    def __init__(
        self, evidence_map: dict[str, list[str]], *, tenant_id: str, corpus_id: str
    ) -> None:
        self._map = evidence_map
        self._tenant_id = tenant_id
        self._corpus_id = corpus_id
        self.calls: list[tuple[str, int]] = []

    def retrieve_evidence(
        self,
        ctx: SecurityContext,
        query: str,
        corpus: CorpusConfig,
        top_k: Any = None,
        *,
        dense_encoder: DenseEncoder,
        sparse_encoder: SparseEncoder,
        iteration: int = 0,
        plan_step_id: Any = None,
    ) -> list[SnapshotEvidence]:
        self.calls.append((query, iteration))
        texts = self._map.get(query, [])
        evidence: list[SnapshotEvidence] = []
        for i, text in enumerate(texts):
            evidence.append(
                SnapshotEvidence(
                    evidence_id=f"ev-{abs(hash((query, i))) % 10_000:04d}",
                    tenant_id=self._tenant_id,
                    corpus_id=self._corpus_id,
                    document_id=f"doc-{iteration}",
                    document_version="v1",
                    source_uri=f"inline://doc-{iteration}",
                    source_filename=f"doc-{iteration}.md",
                    text=text,
                    text_hash=f"h{i}",
                    retrieval_query=query,
                    authority_level=50,
                    retrieved_at=datetime(2024, 1, 1),
                    acl_policy_id="default",
                    policy_version="1.0",
                    retrieval_iteration=iteration,
                )
            )
        return evidence


class _EvalModel:
    """Fake model provider: emits one claim per ``[evidence_id]`` in the prompt."""

    def __init__(self, profile: ModelProfile | None = None) -> None:
        self.profile = profile or ModelProfile(
            provider="fake", model="fake-eval", purpose="synthesis"
        )

    def invoke(self, messages: list[dict[str, str]], **kwargs: Any) -> str:  # noqa: ARG002
        return ""

    def with_structured_output(self, schema: type, **kwargs: Any) -> "_EvalModel._Wrapper":  # noqa: ARG002
        return self._Wrapper(self, schema)

    class _Wrapper:
        def __init__(self, model: "_EvalModel", schema: type) -> None:
            self._model = model
            self._schema = schema

        def invoke(self, messages: list[dict[str, str]], **kwargs: Any) -> Any:
            blob = "\n".join(m.get("content", "") for m in messages)
            ids = list(dict.fromkeys(re.findall(r"\[([A-Za-z0-9_-]+)\]", blob)))
            claims = [
                Claim(claim_id=f"c{i}", text=f"finding from {eid}", evidence_ids=(eid,))
                for i, eid in enumerate(ids)
            ]
            return ClaimExtraction(draft_answer="\n".join(c.text for c in claims), claims=claims)


def _resolve_corpus(corpus_id: str, *, tenant_id: str) -> CorpusConfig:
    return CorpusConfig(
        corpus_id=corpus_id,
        tenant_id=tenant_id,
        name=corpus_id,
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


def build_required_facts(case: EvalCase) -> list[RequiredFact]:
    """Build RequiredFacts for a case from its declared fact descriptions."""
    if case.required_facts:
        return [make_required_fact(d) for d in case.required_facts]
    return []


def run_case(
    case: EvalCase,
    *,
    judge: Judge | None = None,
    max_rounds: int = 3,
    tenant_id: str = "t1",
) -> AnswerEnvelope:
    """Run one eval case through ``answer_with_iteration`` and return the envelope."""
    judge = judge or DeterministicCoverageJudge()
    retriever = _EvalRetriever(case.evidence, tenant_id=tenant_id, corpus_id=case.corpus_id)
    model = _EvalModel()
    service = ChatService(
        retriever=retriever,  # type: ignore[arg-type]
        dense_encoder=_DummyDenseEncoder(),
        sparse_encoder=_DummySparseEncoder(),
        model=model,  # type: ignore[arg-type]
        resolve_corpus=lambda cid: _resolve_corpus(cid, tenant_id=tenant_id),
    )
    ctx = SecurityContext(
        request_id="eval",
        session_id="eval",
        tenant_id=tenant_id,
        user_id="eval-user",
        policy_version="1.0",
    )
    required = build_required_facts(case)
    return service.answer_with_iteration(
        case.query,
        ctx,
        case.corpus_id,
        max_rounds=max_rounds,
        judge=judge,
        required_facts=required,
    )
