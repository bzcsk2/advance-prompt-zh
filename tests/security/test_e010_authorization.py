"""E-010 security: parent second-auth reflects logical delete and ACL tightening.

Confirms that the §12.5 parent second-authorization (E-009) correctly denies a
parent once the document is logically deleted (ParentDeletedError) or its ACL is
tightened to deny a previously-authorized user (ParentNotAuthorizedError).
"""

import pytest

from agentic_rag_enterprise.ingestion.chunker import ParentChildChunker
from agentic_rag_enterprise.ingestion.job import DocumentManager
from agentic_rag_enterprise.retrieval.models import (
    ParentDeletedError,
    ParentNotAuthorizedError,
    RetrievalHit,
)
from agentic_rag_enterprise.retrieval.parent_reader import ParentReader
from agentic_rag_enterprise.security.policy import ResourceAcl

from tests.fixtures import (
    FakeDenseEncoder,
    FakeSparseEncoder,
    acl_payload,
    active_metadata_store,
    make_security_context,
)
from tests.integration.test_e007_end_to_end import _ingest


def _hit_for(child, acl) -> RetrievalHit:
    return RetrievalHit(
        chunk_id=child.child_id,
        parent_id=child.parent_id,
        document_id=child.document_id,
        document_version=child.document_version,
        corpus_id=child.corpus_id,
        tenant_id=child.tenant_id,
        text=child.text,
        score=1.0,
        status="active",
        deprecated=False,
        security_level=acl["security_level"],
        acl_scope=acl["acl_scope"],
        allowed_user_ids=acl.get("allowed_user_ids", []),
        allowed_group_ids=acl.get("allowed_group_ids", []),
        denied_user_ids=acl.get("denied_user_ids", []),
        denied_group_ids=acl.get("denied_group_ids", []),
    )


def _setup():
    acl = acl_payload(tenant_id="t1", acl_scope="tenant", security_level="public")
    vstore, pstore, children = _ingest("eng", "t1", acl)
    mstore = active_metadata_store("t1", "eng", "doc1", "v1")
    mgr = DocumentManager(
        metadata_store=mstore,
        vector_store=vstore,
        parent_store=pstore,
        chunker=ParentChildChunker(),
        dense_encoder=FakeDenseEncoder(),
        sparse_encoder=FakeSparseEncoder(),
    )
    return mgr, pstore, children, acl


def test_deleted_parent_denied_via_parent_reader() -> None:
    mgr, pstore, children, acl = _setup()
    ctx = make_security_context()
    mgr.delete(ctx, corpus_id="eng", document_id="doc1")

    reader = ParentReader(pstore)
    hit = _hit_for(children[0], acl)
    with pytest.raises(ParentDeletedError):
        reader.load_parent_for_hit(hit, ctx)


def test_deny_tightening_revokes_previously_authorized_user() -> None:
    mgr, pstore, children, acl = _setup()
    ctx_u1 = make_security_context(user_id="u1")
    reader = ParentReader(pstore)
    hit = _hit_for(children[0], acl)

    # u1 is authorized before tightening (tenant-scoped).
    assert reader.load_parent_for_hit(hit, ctx_u1) is not None

    # Tighten to restricted and deny u1 explicitly. No re-embedding occurs.
    tight = ResourceAcl(
        tenant_id="t1", security_level="public", acl_scope="restricted",
        allowed_user_ids=[], allowed_group_ids=[], denied_user_ids=["u1"], denied_group_ids=[],
    )
    mgr.tighten_acl(ctx_u1, corpus_id="eng", document_id="doc1", acl=tight)

    # The same user is now denied at the parent second-auth pass.
    with pytest.raises(ParentNotAuthorizedError):
        reader.load_parent_for_hit(hit, ctx_u1)
