"""E-020 versioned eval dataset loader (build plan §14 / M3).

Loads a deterministic, network-free dataset of query → required-facts → per-query
evidence mappings used to drive ``ChatService.answer_with_iteration`` through the
``DeterministicCoverageJudge`` and assert coverage behaviour (including the
``false_sufficient`` and judge-timeout-degradation guards).

The dataset is plain JSON so it can be reviewed and extended without code changes.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

_DATA_DIR = Path(__file__).resolve().parent / "data"


class EvalCase(BaseModel):
    """One eval case.

    ``evidence`` maps a query string (the round-0 query OR a gap-round query,
    which equals a missing fact's normalized description) to the evidence texts
    the fake retriever should return for it.
    """

    id: str
    query: str
    corpus_id: str
    required_facts: list[str] = Field(default_factory=list)
    evidence: dict[str, list[str]] = Field(default_factory=dict)
    expected_overall: str | None = None
    gold_missing_fact_ids: list[str] = Field(default_factory=list)


class EvalDataset(BaseModel):
    version: str
    cases: list[EvalCase] = Field(default_factory=list)


def dataset_path(name: str = "m3_v1") -> Path:
    return _DATA_DIR / f"{name}.json"


def load_dataset(name: str = "m3_v1") -> EvalDataset:
    """Load and validate an eval dataset by name (``m3_v1`` → ``data/m3_v1.json``)."""
    path = dataset_path(name)
    return EvalDataset.model_validate_json(path.read_text(encoding="utf-8"))
