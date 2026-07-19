"""Unit tests for E-023 persistent checkpoint + resume re-authorization.

Covers the two public surfaces of ``storage/checkpoint_store.py``:

* ``RunCheckpoint`` JSON round-trip (via ``MetadataStore`` persistence);
* ``reauthorize_evidence`` — the security-critical re-auth that drops any
  evidence the principal can no longer read on resume (build plan §3623 —
  "ACL 收紧不因旧 Cache/Checkpoint 泄露"). Every fail-closed branch is asserted.
"""

from __future__ import annotations

from datetime import datetime

from agentic_rag_enterprise.corpus.registry import InMemoryCorpusRegistry
from agentic_rag_enterprise.domain.corpus import CorpusConfig
from agentic_rag_enterprise.domain.evidence import Evidence as SnapshotEvidence
from agentic_rag_enterprise.domain.security import SecurityContext
from agentic_rag_enterprise.retrieval.fast_path import FastPathResult, FastPathSufficiency
from agentic_rag_enterprise.storage.checkpoint_store import (
    CHECKPOINT_COMPLETED,
    CHECKPOINT_RUNNING,
    RunCheckpoint,
    reauthorize_evidence,
)
from tests.fixtures import active_metadata_store, make_security_context


def _ctx(tenant_id: str = "t1", user_id: str = "u1") -> SecurityContext:
    return make_security_context(tenant_id=tenant_id, user_id=user_id)


def _evidence(
    evidence_id: str = "e1",
    document_id: str = "doc1",
    document_version: str = "v1",
    corpus_id: str = "eng",
    tenant_id: str = "t1",
) -> SnapshotEvidence:
    return SnapshotEvidence(
        evidence_id=evidence_id,
        tenant_id=tenant_id,
        corpus_id=corpus_id,
        document_id=document_id,
        document_version=document_version,
        source_uri=f"inline://{document_id}",
        source_filename=f"{document_id}.md",
        text="The vacation policy grants 20 days paid leave.",
        text_hash="h-e1",
        retrieval_query="q",
        authority_level=50,
        retrieved_at=datetime(2024, 1, 1),
        acl_policy_id="default",
        policy_version="1.0",
        retrieval_iteration=0,
    )


def _discoverable_registry(corpus_id: str = "eng", tenant_id: str = "t1") -> InMemoryCorpusRegistry:
    corpus = CorpusConfig(
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
    return InMemoryCorpusRegistry([corpus])


# --- RunCheckpoint JSON round-trip -----------------------------------------


def test_run_checkpoint_json_round_trip() -> None:
    ck = RunCheckpoint(
        run_id="r1",
        tenant_id="t1",
        user_id="u1",
        session_id="s1",
        policy_version="1.0",
        query="q",
        corpus_id="eng",
        max_rounds=5,
        round_index=2,
        evidence=(_evidence(evidence_id="e1"), _evidence(evidence_id="e2")),
        prior_queries=["q", "gamma specification"],
        seen_text_hashes=["h-e1", "h-e2"],
        seen_doc_versions=[("doc1", "v1")],
        retrieval_calls=2,
        gap_rounds=2,
        final_reason="max_rounds",
        final_evidence_ids=["e1", "e2"],
    )
    restored = RunCheckpoint.from_json(ck.to_json())
    assert restored == ck


def test_run_checkpoint_persisted_round_trip() -> None:
    mstore = active_metadata_store("t1", "eng", "doc1", "v1")
    ck = RunCheckpoint(
        run_id="r1",
        tenant_id="t1",
        user_id="u1",
        session_id="s1",
        policy_version="1.0",
        query="q",
        corpus_id="eng",
        max_rounds=5,
        round_index=1,
        evidence=(_evidence("e1"),),
        final_evidence_ids=["e1"],
        first_result=FastPathResult(
            query="q",
            corpus_id="eng",
            tenant_id="t1",
            sufficiency=FastPathSufficiency.SUFFICIENT,
            evidence=(_evidence("e1"),),
            stop_reason="evidence_found",
        ),
    )
    mstore.save_run_checkpoint(ck)
    loaded = mstore.load_run_checkpoint("r1")
    assert loaded is not None
    assert loaded.run_id == "r1"
    assert loaded.tenant_id == "t1"
    # The status column is owned by the persistence methods (not the model).
    row = mstore._conn.execute(
        "SELECT status FROM run_checkpoints WHERE run_id=?", ("r1",)
    ).fetchone()
    assert row["status"] == CHECKPOINT_RUNNING
    # Denormalized identity columns must match the JSON payload (corruption guard).
    assert loaded.corpus_id == "eng"
    assert loaded.policy_version == "1.0"
    # The nested state round-trips intact.
    assert loaded.evidence == ck.evidence
    assert loaded.first_result == ck.first_result

    mstore.mark_run_checkpoint_done("r1")
    row = mstore._conn.execute(
        "SELECT status FROM run_checkpoints WHERE run_id=?", ("r1",)
    ).fetchone()
    assert row["status"] == CHECKPOINT_COMPLETED


# --- reauthorize_evidence --------------------------------------------------


def test_reauthorize_keeps_when_authorized() -> None:
    mstore = active_metadata_store("t1", "eng", "doc1", "v1")
    kept, reason = reauthorize_evidence(
        _evidence(), _ctx(), metadata_store=mstore, registry=_discoverable_registry()
    )
    assert kept is True
    assert reason == "authorized"


def test_reauthorize_drops_on_acl_tighten() -> None:
    # E-009/E-010 ACL tightening: the active doc's ACL narrows to a different
    # user. The principal (u1) can no longer read doc1 -> the evidence is dropped.
    mstore = active_metadata_store("t1", "eng", "doc1", "v1")
    mstore.update_document_acl(
        "t1",
        "eng",
        "doc1",
        "v1",
        security_level="public",
        acl_scope="restricted",
        allowed_user_ids=["other"],
        allowed_group_ids=[],
        denied_user_ids=[],
        denied_group_ids=[],
    )
    kept, reason = reauthorize_evidence(
        _evidence(), _ctx(user_id="u1"), metadata_store=mstore, registry=_discoverable_registry()
    )
    assert kept is False
    assert reason == "acl_denied"


def test_reauthorize_drops_when_document_deleted() -> None:
    # Evidence references a document with no active version -> dropped.
    mstore = active_metadata_store("t1", "eng", "doc1", "v1")
    kept, reason = reauthorize_evidence(
        _evidence(document_id="doc-nowhere"),
        _ctx(),
        metadata_store=mstore,
        registry=_discoverable_registry(),
    )
    assert kept is False
    assert reason == "document_inactive_or_deleted"


def test_reauthorize_drops_when_version_superseded() -> None:
    # A re-versioned document: the evidence still points at the old version.
    mstore = active_metadata_store("t1", "eng", "doc1", "v1")
    kept, reason = reauthorize_evidence(
        _evidence(document_version="v0"),
        _ctx(),
        metadata_store=mstore,
        registry=_discoverable_registry(),
    )
    assert kept is False
    assert reason == "version_superseded"


def test_reauthorize_drops_when_corpus_undiscoverable() -> None:
    # The corpus is no longer discoverable for the principal.
    mstore = active_metadata_store("t1", "eng", "doc1", "v1")
    foreign_registry = InMemoryCorpusRegistry(
        [
            CorpusConfig(
                corpus_id="other",
                tenant_id="t2",
                name="Other",
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
        ]
    )
    kept, reason = reauthorize_evidence(
        _evidence(), _ctx(), metadata_store=mstore, registry=foreign_registry
    )
    assert kept is False
    assert reason == "corpus_not_discoverable"


def test_reauthorize_drops_when_discoverability_flag_off() -> None:
    # can_discover_corpus gates separately from registry.get: an explicit
    # allowed_corpus_ids list that omits the corpus must drop the evidence.
    mstore = active_metadata_store("t1", "eng", "doc1", "v1")
    ctx = _ctx().model_copy(update={"allowed_corpus_ids": ["finance"]})
    kept, reason = reauthorize_evidence(
        _evidence(), ctx, metadata_store=mstore, registry=_discoverable_registry()
    )
    assert kept is False
    assert reason == "corpus_not_discoverable"
