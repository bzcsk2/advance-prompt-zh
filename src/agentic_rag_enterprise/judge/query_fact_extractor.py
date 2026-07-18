"""E-019 deterministic Required-Fact extractor (build plan §7.7 / §14.1).

Turns a free-text user query into a list of candidate ``RequiredFact``s by a
light wh-/clause decomposition. This is the Internal-MVP, network-free version:
no LLM. When the caller already supplies an explicit list of required facts
(e.g. an eval dataset or an upstream request), those are returned verbatim — the
extractor never invents facts to replace supplied ones.

The deterministic extractor is intentionally simple and conservative: it only
splits on obvious clause boundaries (sentence punctuation, " and ", " or ") and
normalizes each fragment. Real semantic decomposition is deferred to an LLM
helper behind the same seam.
"""

from __future__ import annotations

import hashlib
import re

from agentic_rag_enterprise.judge.models import RequiredFact

# Split points: sentence/question punctuation, the coordinating conjunctions
# "and" / "or" (surrounded by whitespace), and newlines.
_CLAUSE_SPLIT = re.compile(r"[.?!;\n]+|\s+and\s+|\s+or\s+", re.IGNORECASE)


def _normalize(description: str) -> str:
    """Normalize a fact description for stable id derivation (§7.7)."""
    return description.strip().strip(" ?!.").lower()


def _fact_id(description: str) -> str:
    """Stable id derived from the normalized description (§7.7)."""
    digest = hashlib.sha256(_normalize(description).encode("utf-8")).hexdigest()
    return f"fact_{digest[:12]}"


def make_required_fact(description: str, *, required: bool = True) -> RequiredFact:
    """Build a single ``RequiredFact`` for ``description`` (normalized id).

    Used by callers that supply explicit facts (eval datasets, requests) rather
    than relying on heuristic query decomposition. ``_fact_id`` keeps the id
    stable so datasets can reference the same fact across runs.
    """
    return RequiredFact(
        fact_id=_fact_id(description),
        description=_normalize(description),
        required=required,
    )


class DeterministicQueryFactExtractor:
    """Decompose a query into candidate RequiredFacts (heuristic, no LLM)."""

    name = "deterministic"

    def extract(
        self,
        query: str,
        *,
        supplied: list[RequiredFact] | None = None,
    ) -> list[RequiredFact]:
        """Return required facts.

        Args:
            query: The user question (used only when ``supplied`` is not given).
            supplied: An explicit list of facts from the caller/eval dataset.
                When present it is returned unchanged — supplied facts always
                win over heuristic decomposition.

        Returns:
            A list of :class:`RequiredFact`. Empty only when ``supplied`` is an
            empty list or the query decomposes to nothing matchable.
        """
        if supplied is not None:
            return list(supplied)

        facts: list[RequiredFact] = []
        seen_ids = set()
        for raw in _CLAUSE_SPLIT.split(query):
            description = _normalize(raw)
            if not description:
                continue
            fact_id = _fact_id(description)
            if fact_id in seen_ids:
                continue
            seen_ids.add(fact_id)
            facts.append(
                RequiredFact(
                    fact_id=fact_id,
                    description=description,
                    required=True,
                )
            )
        return facts
