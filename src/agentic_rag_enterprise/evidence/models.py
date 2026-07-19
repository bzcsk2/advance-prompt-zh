"""E-021 conflict + structured-assertion models (build plan §15.3 / §15.4).

These models are produced by the ``ConflictResolver`` (``evidence/conflict_resolver.py``)
and carried on the ``AnswerEnvelope`` via ``conflict_report``. They deliberately
reuse only fields that already exist on ``domain.evidence.Evidence`` — no new
``Evidence`` field is introduced (issue scope).

Conflict detection is **conservative and deterministic** (issue #3): candidate
conflicts are created only under strict conditions (same-``document_id`` version
divergence, or a structured ``assertion`` parser extracting a *different* value
on the *same* key with overlapping effective time). Differing full ``text`` alone
**never** creates a candidate conflict.
"""

from __future__ import annotations

import re
from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict

from agentic_rag_enterprise.domain.temporal import TemporalScope


class ConflictType(str, Enum):
    """The category of a detected candidate conflict."""

    VALUE_CONFLICT = "value_conflict"  # same topic+key, different asserted value
    VERSION_CONFLICT = "version_conflict"  # same document_id, different document_version
    TIME_CONFLICT = "time_conflict"  # same topic, different effective windows
    SCOPE_CONFLICT = "scope_conflict"  # different key/subject, complementary not contradictory
    POLICY_CONFLICT = "policy_conflict"  # contradicts a formal policy / authoritative doc


class ConflictStatus(str, Enum):
    """Resolver-only verdict — the resolver cannot judge sufficiency (issue #2)."""

    NONE = "none"  # no candidate conflict at all
    RESOLVED = "resolved"  # every candidate auto-resolved (version/time/authority/scope)
    CONTRADICTED = "contradicted"  # >=1 candidate could not be resolved


class ConflictResolution(str, Enum):
    """How a candidate conflict was disposed of."""

    AUTO_VERSION = "auto_resolved_version"  # rule 1
    AUTO_TIME = "auto_resolved_time"  # rule 2
    AUTO_AUTHORITY = "auto_resolved_authority"  # rule 3
    AUTO_SCOPE = "auto_resolved_scope"  # scope: keep both, distinct
    UNRESOLVED = "unresolved"  # → CONTRADICTED


class AssertionExtraction(BaseModel):
    """Deterministic structured value extracted from one Evidence (issue #3)."""

    model_config = ConfigDict(frozen=True)

    is_structured: bool
    value: str | None = None  # normalized, e.g. "v2", "true", "60s"
    value_kind: Literal["version", "boolean", "quantity", "key_value"] | None = None
    key: str | None = None  # for key_value: the LHS key


class SourceRef(BaseModel):
    """Immutable pointer back to the conflicting Evidence (build plan §16.6)."""

    model_config = ConfigDict(frozen=True)

    evidence_id: str
    corpus_id: str
    document_id: str
    document_version: str
    section_path: tuple[str, ...] = ()
    source_filename: str = ""
    authority_level: int
    effective_from: datetime | None = None
    effective_to: datetime | None = None
    is_temporary: bool = False  # effective_to is set → bounded / temporary, not permanent


class ConflictFinding(BaseModel):
    """One candidate conflict and its disposition."""

    model_config = ConfigDict(frozen=True)

    conflict_id: str
    conflict_type: ConflictType
    topic_key: str  # deterministic grouping key (e.g. normalized query)
    sources: tuple[SourceRef, ...]  # every involved snapshot, with provenance
    resolvable: bool
    resolution: ConflictResolution
    chosen_evidence_ids: tuple[str, ...] = ()  # empty when UNRESOLVED
    explanation: str = ""


class ConflictReport(BaseModel):
    """The resolver's output — a resolver-only verdict, never a sufficiency call."""

    model_config = ConfigDict(frozen=True)

    scope: TemporalScope
    conflict_status: ConflictStatus  # resolver-only: none / resolved / contradicted
    findings: tuple[ConflictFinding, ...]
    resolved_evidence_ids: tuple[str, ...]  # evidence to feed downstream synthesis
    contradicted_fact_ids: tuple[str, ...] = ()


# --- deterministic structured-assertion parser (no LLM / NER) ---------------

# Precedence: version → key_value → quantity → boolean. key_value is checked
# before quantity/boolean so a "key: value" pair keeps its key (needed to tell
# SCOPE_CONFLICT apart from VALUE_CONFLICT).
_VERSION_RE = re.compile(r"(?:v|version|版本)\s*[:=]?\s*(\d+(?:\.\d+)*)")
_KEY_VALUE_RE = re.compile(r"([A-Za-z_一-龥]+)\s*[:：]\s*(\S+)")
_QUANTITY_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(s|ms|seconds|minutes|分钟|秒|小时|mb|gb|kb|%|个|条)")
_BOOLEAN_RE = re.compile(r"enabled|disabled|true|false|开启|关闭|启用|停用|是|否")


def extract_assertion(text: str) -> AssertionExtraction:
    """Extract a single deterministic structured assertion from evidence text.

    Returns ``is_structured=False`` when none of the explicit forms match — the
    resolver then treats the evidence as *pass-through* (no candidate conflict
    from differing free text alone, issue #3).
    """
    if not text:
        return AssertionExtraction(is_structured=False)

    version = _VERSION_RE.search(text)
    if version:
        return AssertionExtraction(
            is_structured=True,
            value=f"v{version.group(1)}",
            value_kind="version",
            key="version",
        )

    key_value = _KEY_VALUE_RE.search(text)
    if key_value:
        return AssertionExtraction(
            is_structured=True,
            value=key_value.group(2),
            value_kind="key_value",
            key=key_value.group(1),
        )

    quantity = _QUANTITY_RE.search(text)
    if quantity:
        return AssertionExtraction(
            is_structured=True,
            value=f"{quantity.group(1)}{quantity.group(2)}",
            value_kind="quantity",
        )

    boolean = _BOOLEAN_RE.search(text)
    if boolean:
        return AssertionExtraction(
            is_structured=True,
            value=boolean.group(0).lower(),
            value_kind="boolean",
        )

    return AssertionExtraction(is_structured=False)


def normalize_topic_key(query: str) -> str:
    """Deterministic grouping key: whitespace-collapsed, lower-cased, punctuation-stripped."""
    collapsed = re.sub(r"\s+", " ", (query or "").strip()).lower()
    return re.sub(r"[^\w一-龥]+", "", collapsed)
