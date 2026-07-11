"""Security tests: parent second authorization (E-007).

Every parent read must re-establish identity (tenant/corpus/document/version),
lifecycle (active, non-deprecated), ACL consistency, and the
``resource_passes_filter`` decision. Any violation fails closed with
:class:`ParentAuthorizationError`. Model/Tool-supplied parent ids that are not
present in the store are rejected (no guessing).
"""

import pytest

from agentic_rag_enterprise.retrieval.models import ParentAuthorizationError, RetrievalHit
from agentic_rag_enterprise.retrieval.parent_reader import ParentReader
from agentic_rag_enterprise.storage.parent_store import ParentStore
from tests.fixtures import (
    acl_payload,
    make_parent_chunk,
    make_security_context,
)

PARENT_ID = "p1234567890abcdef"
ACL = acl_payload(tenant_id="t1", acl_scope="tenant", security_level="public")


def _store_with_parent() -> ParentStore:
    store = ParentStore()
    store.put(
        make_parent_chunk(
            PARENT_ID,
            "full parent text",
            tenant_id="t1",
            corpus_id="eng",
            document_id="d1",
            document_version="v1",
            acl=ACL,
        )
    )
    return store


def _hit(**overrides: object) -> RetrievalHit:
    defaults = dict(
        chunk_id="c1",
        parent_id=PARENT_ID,
        document_id="d1",
        document_version="v1",
        corpus_id="eng",
        tenant_id="t1",
        text="child text",
        score=1.0,
        status="active",
        deprecated=False,
        security_level="public",
        acl_scope="tenant",
        allowed_user_ids=[],
        allowed_group_ids=[],
        denied_user_ids=[],
        denied_group_ids=[],
    )
    defaults.update(overrides)
    return RetrievalHit(**defaults)


def test_second_authorization_succeeds() -> None:
    reader = ParentReader(_store_with_parent())
    parent = reader.load_parent_for_hit(_hit(), make_security_context())
    assert parent.parent_id == PARENT_ID
    assert parent.content == "full parent text"
    assert parent.document_version == "v1"


def test_guessed_parent_id_rejected() -> None:
    reader = ParentReader(_store_with_parent())
    import pytest

    with pytest.raises(ParentAuthorizationError):
        reader.load_parent_for_hit(_hit(parent_id="deadbeefdeadbeef"), make_security_context())


def test_tenant_mismatch_fails_closed() -> None:
    reader = ParentReader(_store_with_parent())
    import pytest

    with pytest.raises(ParentAuthorizationError):
        reader.load_parent_for_hit(_hit(tenant_id="t2"), make_security_context())


def test_corpus_mismatch_fails_closed() -> None:
    reader = ParentReader(_store_with_parent())
    import pytest

    with pytest.raises(ParentAuthorizationError):
        reader.load_parent_for_hit(_hit(corpus_id="other"), make_security_context())


def test_document_identity_mismatch_fails_closed() -> None:
    reader = ParentReader(_store_with_parent())
    import pytest

    with pytest.raises(ParentAuthorizationError):
        reader.load_parent_for_hit(_hit(document_id="d2"), make_security_context())


def test_document_version_mismatch_fails_closed() -> None:
    reader = ParentReader(_store_with_parent())
    import pytest

    with pytest.raises(ParentAuthorizationError):
        reader.load_parent_for_hit(_hit(document_version="v2"), make_security_context())


def test_inactive_parent_rejected() -> None:
    store = ParentStore()
    store.put(
        make_parent_chunk(
            PARENT_ID,
            "x",
            tenant_id="t1",
            corpus_id="eng",
            document_id="d1",
            document_version="v1",
            acl=ACL,
            status="deleted",
        )
    )
    reader = ParentReader(store)
    import pytest

    with pytest.raises(ParentAuthorizationError):
        reader.load_parent_for_hit(_hit(), make_security_context())


def test_deprecated_parent_rejected() -> None:
    store = ParentStore()
    store.put(
        make_parent_chunk(
            PARENT_ID,
            "x",
            tenant_id="t1",
            corpus_id="eng",
            document_id="d1",
            document_version="v1",
            acl=ACL,
            deprecated=True,
        )
    )
    reader = ParentReader(store)
    import pytest

    with pytest.raises(ParentAuthorizationError):
        reader.load_parent_for_hit(_hit(), make_security_context())


def test_child_parent_acl_mismatch_fails_closed() -> None:
    reader = ParentReader(_store_with_parent())
    import pytest

    # Child claims restricted scope while stored parent is tenant scope.
    with pytest.raises(ParentAuthorizationError):
        reader.load_parent_for_hit(
            _hit(acl_scope="restricted", allowed_user_ids=["u1"]), make_security_context()
        )


def test_resource_passes_filter_deny_fails_closed() -> None:
    # Restricted parent that does not allow this user/group.
    acl = acl_payload(tenant_id="t1", acl_scope="restricted", security_level="public")
    store = ParentStore()
    store.put(
        make_parent_chunk(
            PARENT_ID,
            "x",
            tenant_id="t1",
            corpus_id="eng",
            document_id="d1",
            document_version="v1",
            acl=acl,
        )
    )
    reader = ParentReader(store)

    with pytest.raises(ParentAuthorizationError):
        reader.load_parent_for_hit(_hit(acl_scope="restricted"), make_security_context())


def _valid_parent_metadata() -> dict:
    """A fully-populated, valid authorization metadata block."""
    parent = make_parent_chunk(
        PARENT_ID,
        "x",
        tenant_id="t1",
        corpus_id="eng",
        document_id="d1",
        document_version="v1",
        acl=ACL,
    )
    return dict(parent.metadata)


def _store_with_metadata(meta: dict) -> ParentStore:
    parent = make_parent_chunk(
        PARENT_ID,
        "x",
        tenant_id="t1",
        corpus_id="eng",
        document_id="d1",
        document_version="v1",
        acl=ACL,
    )
    store = ParentStore()
    store.put(parent.model_copy(update={"metadata": meta}))
    return store


@pytest.mark.parametrize(
    "drop_key",
    [
        "status",
        "deprecated",
        "security_level",
        "acl_scope",
        "allowed_user_ids",
        "denied_user_ids",
    ],
)
def test_missing_required_field_rejected(drop_key: str) -> None:
    meta = _valid_parent_metadata()
    del meta[drop_key]
    reader = ParentReader(_store_with_metadata(meta))
    with pytest.raises(ParentAuthorizationError):
        reader.load_parent_for_hit(_hit(), make_security_context())


@pytest.mark.parametrize(
    "key,value",
    [
        ("deprecated", "false"),  # string, not bool
        ("status", 1),  # not a string
        ("acl_scope", "public"),  # not tenant|restricted
        ("allowed_user_ids", "u1"),  # not a list
        ("allowed_group_ids", [1, 2]),  # list with non-str elements
        ("denied_user_ids", {"u1"}),  # set, not list[str]
    ],
)
def test_malformed_field_type_rejected(key: str, value: object) -> None:
    meta = _valid_parent_metadata()
    meta[key] = value
    reader = ParentReader(_store_with_metadata(meta))
    with pytest.raises(ParentAuthorizationError):
        reader.load_parent_for_hit(_hit(), make_security_context())
