"""Unit tests for ParentStore bulk document helpers (E-010)."""

from agentic_rag_enterprise.storage.parent_store import ParentStore

from tests.fixtures import acl_payload, make_parent_chunk


def _store() -> ParentStore:
    s = ParentStore()
    s.put(make_parent_chunk("p1", "x", tenant_id="t1", corpus_id="eng", document_id="d1", document_version="v1", acl=acl_payload()))
    s.put(make_parent_chunk("p2", "y", tenant_id="t1", corpus_id="eng", document_id="d1", document_version="v1", acl=acl_payload()))
    s.put(make_parent_chunk("p3", "z", tenant_id="t1", corpus_id="eng", document_id="d2", document_version="v1", acl=acl_payload()))
    return s


def test_deprecate_document_flips_only_target() -> None:
    s = _store()
    s.deprecate_document("d1", "v1")
    assert s.get("p1").metadata["status"] == "inactive"
    assert s.get("p1").metadata["deprecated"] is True
    assert s.get("p2").metadata["deprecated"] is True
    # Other documents untouched.
    assert s.get("p3").metadata["deprecated"] is False


def test_delete_document_removes_only_target() -> None:
    s = _store()
    s.delete_document("d1", "v1")
    assert "p1" not in s and "p2" not in s
    assert "p3" in s


def test_update_acl_document_patches_only_target() -> None:
    s = _store()
    s.update_acl_document("d1", "v1", {"acl_scope": "restricted", "allowed_user_ids": ["u9"]})
    assert s.get("p1").metadata["acl_scope"] == "restricted"
    assert s.get("p1").metadata["allowed_user_ids"] == ["u9"]
    # Other documents untouched.
    assert s.get("p3").metadata["acl_scope"] == "tenant"
