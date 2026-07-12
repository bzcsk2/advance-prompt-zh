"""E-009 integration: parent-store secondary authorization failure semantics.

Full pipeline (chunk -> upsert -> hybrid -> parent 2nd auth) proving that each
build plan §12.5 denial reaches the parent pass with the correct §12.9 code
(``PARENT_NOT_FOUND`` / ``PARENT_NOT_AUTHORIZED`` / ``DOCUMENT_DELETED`` /
``VERSION_MISMATCH``), that denials are counted, and that the user-facing
``RetrievalResult`` never reveals per-parent existence detail.
"""

from tests.fixtures import (
    FakeDenseEncoder,
    FakeSparseEncoder,
    acl_payload,
    active_metadata_store,
    make_security_context,
)
from tests.integration.test_e007_end_to_end import _corpus, _ingest

from agentic_rag_enterprise.retrieval.hybrid import _HybridSearchAdapter
from agentic_rag_enterprise.retrieval.parent_reader import ParentReader
from agentic_rag_enterprise.retrieval.retriever import SecureRetriever


def _retriever(store, pstore) -> SecureRetriever:
    return SecureRetriever(
        _HybridSearchAdapter(store),
        ParentReader(pstore),
        metadata_store=active_metadata_store("t1", "eng", "doc1", "v1"),
    )


def _run(store, pstore):
    return _retriever(store, pstore).retrieve(
        make_security_context(),
        "architecture planner security",
        _corpus(),
        dense_encoder=FakeDenseEncoder(),
        sparse_encoder=FakeSparseEncoder(),
    )


def _parent_ids(children) -> set[str]:
    return {c.parent_id for c in children}


def test_deleted_parent_denied_with_document_deleted_code() -> None:
    acl = acl_payload(tenant_id="t1", acl_scope="tenant", security_level="public")
    store, pstore, children = _ingest("eng", "t1", acl)
    for pid in _parent_ids(children):
        parent = pstore.get(pid)
        assert parent is not None
        pstore.put(parent.model_copy(update={"metadata": {**parent.metadata, "deprecated": True}}))

    result = _run(store, pstore)

    assert result.hits == []
    assert result.denied_parent_count > 0
    assert set(result.denied_reasons) == {"DOCUMENT_DELETED"}
    assert result.denied_reasons["DOCUMENT_DELETED"] == result.denied_parent_count


def test_missing_parent_denied_with_parent_not_found_code() -> None:
    acl = acl_payload(tenant_id="t1", acl_scope="tenant", security_level="public")
    store, pstore, children = _ingest("eng", "t1", acl)
    for pid in _parent_ids(children):
        pstore.delete(pid)

    result = _run(store, pstore)

    assert result.hits == []
    assert set(result.denied_reasons) == {"PARENT_NOT_FOUND"}


def test_version_divergence_denied_with_version_mismatch_code() -> None:
    acl = acl_payload(tenant_id="t1", acl_scope="tenant", security_level="public")
    store, pstore, children = _ingest("eng", "t1", acl)
    for pid in _parent_ids(children):
        parent = pstore.get(pid)
        assert parent is not None
        pstore.put(parent.model_copy(update={"document_version": "v9"}))

    result = _run(store, pstore)

    assert result.hits == []
    assert set(result.denied_reasons) == {"VERSION_MISMATCH"}


def test_acl_divergence_denied_with_not_authorized_code() -> None:
    acl = acl_payload(tenant_id="t1", acl_scope="tenant", security_level="public")
    store, pstore, children = _ingest("eng", "t1", acl)
    for pid in _parent_ids(children):
        parent = pstore.get(pid)
        assert parent is not None
        tightened = {**parent.metadata, "allowed_user_ids": ["someone-else"]}
        pstore.put(parent.model_copy(update={"metadata": tightened}))

    result = _run(store, pstore)

    assert result.hits == []
    assert set(result.denied_reasons) == {"PARENT_NOT_AUTHORIZED"}


def test_result_does_not_leak_parent_existence() -> None:
    acl = acl_payload(tenant_id="t1", acl_scope="tenant", security_level="public")
    store, pstore, children = _ingest("eng", "t1", acl)
    for pid in _parent_ids(children):
        pstore.delete(pid)

    result = _run(store, pstore)

    # Internal observability is preserved (telemetry path).
    assert result.denied_reasons["PARENT_NOT_FOUND"] > 0

    # But serialization must NOT carry the denial reasons nor any per-parent id
    # (build plan §12.9: avoid leaking whether an unauthorized resource exists).
    dumped = result.model_dump()
    assert set(dumped) == {"hits", "denied_parent_count"}
    assert "denied_reasons" not in dumped
    assert "PARENT_NOT_FOUND" not in result.model_dump_json()
    assert "PARENT_NOT_AUTHORIZED" not in result.model_dump_json()
    assert "DOCUMENT_DELETED" not in result.model_dump_json()
    assert "VERSION_MISMATCH" not in result.model_dump_json()
    assert dumped["hits"] == []
    for value in _parent_ids(children):
        assert value not in repr(dumped)
