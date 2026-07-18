"""E-018 Tool / ToolSpec / ToolRegistry contracts + RetrieverTool (build plan §13.4, §9, §12).

Defines the ``Tool`` protocol (what the Executor calls), the ``ToolSpec`` model
(describes the Tool's schema for binding validation), and the ``ToolRegistry``
protocol (lookup by ``step_type + capability_id``).

For M5 the only registered Tool is ``RetrieverTool`` wrapping
:class:`~agentic_rag_enterprise.retrieval.retriever.SecureRetriever`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from agentic_rag_enterprise.corpus.registry import CorpusRegistry
from agentic_rag_enterprise.domain.evidence import Evidence as SnapshotEvidence
from agentic_rag_enterprise.domain.security import SecurityContext
from agentic_rag_enterprise.planner.models import OutputSchemaId, PlanStep
from agentic_rag_enterprise.retrieval.retriever import SecureRetriever
from agentic_rag_enterprise.storage.vector_store import DenseEncoder, SparseEncoder

# ---------------------------------------------------------------------------
# Internal convention for resolved_inputs keys
# ---------------------------------------------------------------------------
# The Executor pre-resolves the step query (substituting template placeholders
# against completed upstream outputs) and injects it into resolved_inputs under
# this key.  All Tools must read the actual query string from this key.
_RESOLVED_QUERY_KEY = "__query__"


class TypedStepOutput(BaseModel):
    """Structured output from a single Tool execution (contract §10)."""

    model_config = ConfigDict(frozen=True)

    outputs: dict[str, object]
    evidence_ids: tuple[str, ...] = ()
    schema_id: OutputSchemaId


@runtime_checkable
class Tool(Protocol):
    """Protocol that every executable Tool must satisfy.

    The Executor calls ``execute_step`` after resolving bindings and reserving
    budget.  The Tool should NOT perform its own budget accounting.
    """

    def execute_step(
        self,
        step: PlanStep,
        resolved_inputs: Mapping[str, object],
        ctx: SecurityContext,
    ) -> TypedStepOutput: ...


class ToolSpec(BaseModel):
    """Metadata describing a Tool's input/output schema (contract §4a).

    Used by the Executor to:
    - validate ``resolved_inputs`` field requiredness (``is_required()``);
    - select the correct output model per ``PlanStep.output_schema_id``;
    - decide which exceptions are transient (retryable).
    """

    model_config = ConfigDict(frozen=True)

    step_type: str
    capability_id: str

    input_model: type[BaseModel]
    output_models: Mapping[OutputSchemaId, type[BaseModel]]

    retryable_errors: frozenset[type[Exception]]


@runtime_checkable
class ToolRegistry(Protocol):
    """Lookup service for Tools (contract §10).

    The Executor calls ``get(step.step_type, step.capability_id)`` to obtain
    both the ``Tool`` implementation and its ``ToolSpec`` metadata.
    """

    def get(self, step_type: str, capability_id: str) -> tuple[Tool, ToolSpec]: ...


# ---------------------------------------------------------------------------
# M5 RetrieverTool
# ---------------------------------------------------------------------------


class RetrieverTool:
    """Concrete ``Tool`` that wraps ``SecureRetriever.retrieve_evidence``.

    Resolves one or more target corpora from the step, calls
    ``retrieve_evidence`` per corpus, merges the evidence lists, and applies
    the deterministic projection (contract §10a).

    Constructor receives long-lived service dependencies:
    - ``retriever`` — the ``SecureRetriever`` instance
    - ``corpus_registry`` — the security-aware :class:`CorpusRegistry` for
      resolving ``corpus_id`` → ``CorpusConfig``
    - ``dense_encoder``, ``sparse_encoder`` — encoding callables required by
      ``retriever.retrieve_evidence``

    The Executor is responsible for **budget** (``try_reserve``) and
    **retry** logic — this Tool only performs the retrieval + projection.
    """

    def __init__(
        self,
        retriever: SecureRetriever,
        corpus_registry: CorpusRegistry,
        dense_encoder: DenseEncoder,
        sparse_encoder: SparseEncoder,
    ) -> None:
        self._retriever = retriever
        self._corpus_registry = corpus_registry
        self._dense_encoder = dense_encoder
        self._sparse_encoder = sparse_encoder

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def execute_step(
        self,
        step: PlanStep,
        resolved_inputs: Mapping[str, object],
        ctx: SecurityContext,
    ) -> TypedStepOutput:
        """Run the retrieval step and project the output.

        ``resolved_inputs`` must contain a ``"__query__"`` key with the
        pre-resolved query string (the Executor performs template substitution).
        """
        query = _extract_query(resolved_inputs)

        # Resolve corpus configs (throws CorpusNotDiscoverableError if any
        # corpus is unauthorised — that's a security binding failure).
        corpus_configs = [self._corpus_registry.get(cid, ctx) for cid in step.target_corpus_ids]

        # Retrieve per corpus.
        per_corpus: dict[str, list[SnapshotEvidence]] = {}
        for cc in corpus_configs:
            ev_list = self._retriever.retrieve_evidence(
                ctx,
                query,
                cc,
                dense_encoder=self._dense_encoder,
                sparse_encoder=self._sparse_encoder,
                plan_step_id=step.step_id,
            )
            per_corpus[cc.corpus_id] = ev_list

        # Merge all evidence lists into a single deterministic order.
        merged = _merge_evidence_per_corpus(per_corpus)

        # Project according to the output schema.
        outputs = _project(merged, step.output_schema_id)
        evidence_ids = tuple(e.evidence_id for e in merged)

        return TypedStepOutput(
            outputs=outputs,
            evidence_ids=evidence_ids,
            schema_id=step.output_schema_id,
        )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _extract_query(resolved_inputs: Mapping[str, object]) -> str:
    """Read the pre-resolved query from ``resolved_inputs``."""
    q = resolved_inputs.get(_RESOLVED_QUERY_KEY)
    if not isinstance(q, str) or not q.strip():
        raise ValueError("resolved_inputs must contain a non-empty __query__ string")
    return q


def _merge_evidence_per_corpus(
    per_corpus: dict[str, list[SnapshotEvidence]],
) -> list[SnapshotEvidence]:
    """Merge evidence from multiple corpora with first-occurrence dedup.

    Iterates corpora in ascending ``corpus_id`` order (deterministic), evidence
    in retrieval order.  The first occurrence of an ``evidence_id`` wins;
    duplicates are dropped.
    """
    seen: set[str] = set()
    merged: list[SnapshotEvidence] = []
    for corpus_id in sorted(per_corpus):
        for ev in per_corpus[corpus_id]:
            if ev.evidence_id not in seen:
                seen.add(ev.evidence_id)
                merged.append(ev)
    return merged


def _project(
    evidence: Sequence[SnapshotEvidence],
    schema_id: OutputSchemaId,
) -> dict[str, object]:
    """Apply the deterministic projection (contract §10a).

    Sort: ``retrieval_score`` descending (``None`` → ``0.0``), then
    ``authority_level`` descending, then ``evidence_id`` ascending.
    """
    if not evidence:
        return _empty_projection(schema_id)

    sorted_ev = _sort_evidence(evidence)
    top = sorted_ev[0]

    if schema_id == "entity":
        return {
            "entity_text": top.text,
            "corpus_id": top.corpus_id,
            "document_id": top.document_id,
            "section_path": top.section_path,
            "authority_level": top.authority_level,
        }
    if schema_id == "spec":
        return {
            "spec_text": top.text,
            "corpus_id": top.corpus_id,
            "document_id": top.document_id,
            "metadata": {
                "authority_level": top.authority_level,
                "retrieval_score": top.retrieval_score,
                "section_path": top.section_path,
            },
        }
    if schema_id == "comparison":
        return {
            "items": [
                {
                    "corpus_id": e.corpus_id,
                    "text": e.text,
                    "authority_level": e.authority_level,
                }
                for e in sorted_ev
            ],
            "evidence_ids": [e.evidence_id for e in sorted_ev],
        }
    # schema_id == "intermediate"
    return {
        "texts": [e.text for e in sorted_ev],
        "evidence_ids": [e.evidence_id for e in sorted_ev],
    }


def _sort_evidence(
    evidence: Sequence[SnapshotEvidence],
) -> list[SnapshotEvidence]:
    """Deterministic sort per §10a frozen rules."""

    def _key(ev: SnapshotEvidence) -> tuple[float, int, str]:
        score = ev.retrieval_score if ev.retrieval_score is not None else 0.0
        return (-score, -ev.authority_level, ev.evidence_id)

    return sorted(evidence, key=_key)


def _empty_projection(schema_id: OutputSchemaId) -> dict[str, object]:
    """Return schema-appropriate empty outputs (contract §10a empty-list rule)."""
    if schema_id == "entity":
        return {
            "entity_text": "",
            "corpus_id": "",
            "document_id": "",
            "section_path": (),
            "authority_level": 0,
        }
    if schema_id == "spec":
        return {
            "spec_text": "",
            "corpus_id": "",
            "document_id": "",
            "metadata": {
                "authority_level": 0,
                "retrieval_score": None,
                "section_path": [],
            },
        }
    if schema_id == "comparison":
        return {"items": [], "evidence_ids": []}
    # intermediate
    return {"texts": [], "evidence_ids": []}
