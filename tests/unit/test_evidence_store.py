"""Unit tests for the Evidence snapshot store (build plan §12.8)."""

import os
import tempfile
from datetime import datetime, timezone

from agentic_rag_enterprise.domain.evidence import Evidence
from agentic_rag_enterprise.domain.security import SecurityContext
from agentic_rag_enterprise.security.policy import ResourceAcl
from agentic_rag_enterprise.storage.evidence_store import (
    EvidenceAccessLevel,
    EvidenceSnapshotStore,
)


def _evidence(**overrides) -> Evidence:
    base = dict(
        evidence_id="ev-1",
        tenant_id="t1",
        corpus_id="eng",
        document_id="doc1",
        document_version="v1",
        source_uri="inline://doc1",
        source_filename="doc1.md",
        parent_id="p1",
        child_chunk_id="c1",
        text="The system routes queries across corpora.",
        text_hash="abc",
        retrieval_query="how does routing work",
        retrieved_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        acl_policy_id="default",
        policy_version="1.0",
        retrieval_iteration=0,
        authority_level=50,
    )
    base.update(overrides)
    return Evidence(**base)


def _ctx(**overrides) -> SecurityContext:
    base = dict(
        request_id="r",
        session_id="s",
        tenant_id="t1",
        user_id="u1",
        allowed_security_levels=["public", "internal"],
        allowed_corpus_ids=None,
        policy_version="1.0",
        is_admin=False,
        permissions=[],
    )
    base.update(overrides)
    return SecurityContext(**base)


def _acl(**overrides) -> ResourceAcl:
    base = dict(
        tenant_id="t1",
        security_level="internal",
        acl_scope="tenant",
        allowed_user_ids=[],
        allowed_group_ids=[],
        denied_user_ids=[],
        denied_group_ids=[],
    )
    base.update(overrides)
    return ResourceAcl(**base)


def _store() -> EvidenceSnapshotStore:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    return EvidenceSnapshotStore(path)


def test_save_then_get_full_when_authorized() -> None:
    store = _store()
    ev = _evidence()
    store.save(ev, source_acl=_acl())
    access = store.get("ev-1", _ctx())
    assert access.level is EvidenceAccessLevel.FULL
    assert access.evidence is not None
    assert access.evidence.text == ev.text
    assert access.evidence.evidence_id == "ev-1"


def test_save_is_idempotent() -> None:
    store = _store()
    ev = _evidence()
    store.save(ev, source_acl=_acl())
    store.save(ev, source_acl=_acl())  # re-save must not duplicate
    assert store.count("t1") == 1
    assert store.exists("ev-1")


def test_cross_tenant_read_denied() -> None:
    store = _store()
    store.save(_evidence(), source_acl=_acl())
    access = store.get("ev-1", _ctx(tenant_id="t2"))
    assert access.level is EvidenceAccessLevel.DENIED
    assert access.evidence is None


def test_redacted_when_source_acl_revoked() -> None:
    # Source ACL is restricted to a different user; the requester no longer has
    # access. The body must be withheld but provenance preserved (§12.8).
    store = _store()
    store.save(
        _evidence(),
        source_acl=_acl(acl_scope="restricted", allowed_user_ids=["other"]),
    )
    access = store.get("ev-1", _ctx(user_id="u1"))
    assert access.level is EvidenceAccessLevel.REDACTED
    assert access.evidence is not None
    assert access.evidence.text == ""  # body withheld
    assert access.evidence.text_hash == "abc"  # provenance kept
    assert access.evidence.evidence_id == "ev-1"


def test_audit_grant_reads_body_and_emits_event() -> None:
    events: list[dict] = []
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    store = EvidenceSnapshotStore(path, audit_callback=events.append)
    store.save(
        _evidence(),
        source_acl=_acl(acl_scope="restricted", allowed_user_ids=["other"]),
    )
    ctx = _ctx(user_id="u1", permissions=["audit:evidence:read"])
    access = store.get("ev-1", ctx)
    assert access.level is EvidenceAccessLevel.FULL
    assert access.evidence is not None
    assert access.evidence.text == "The system routes queries across corpora."
    assert events  # audit callback fired
    assert events[0]["action"] == "audit_read"


def test_snapshot_body_is_immutable_across_re_saves() -> None:
    store = _store()
    store.save(_evidence(text="original body", text_hash="h1"), source_acl=_acl())
    # No update path exists; re-saving the same id keeps the first write.
    store.save(_evidence(text="tampered body", text_hash="h2"), source_acl=_acl())
    access = store.get("ev-1", _ctx())
    assert access.evidence is not None
    assert access.evidence.text == "original body"
    assert access.evidence.text_hash == "h1"


def test_missing_evidence_denied() -> None:
    store = _store()
    access = store.get("nope", _ctx())
    assert access.level is EvidenceAccessLevel.DENIED
