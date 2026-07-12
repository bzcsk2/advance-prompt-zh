"""E-010 integration: full ingest -> retrieve -> update -> delete -> purge -> re-ingest.

Drives the same fixture through the complete M1 exit gate (build plan §3530)
using the real ``DocumentManager`` mutation API over the real Qdrant + parent +
metadata stores.
"""


from agentic_rag_enterprise.ingestion.chunker import ParentChildChunker
from agentic_rag_enterprise.ingestion.job import DocumentManager, IngestionRequest
from agentic_rag_enterprise.retrieval.hybrid import _HybridSearchAdapter
from agentic_rag_enterprise.retrieval.parent_reader import ParentReader
from agentic_rag_enterprise.retrieval.retriever import SecureRetriever
from agentic_rag_enterprise.security.policy import ResourceAcl

from tests.fixtures import (
    FakeDenseEncoder,
    FakeSparseEncoder,
    acl_payload,
    active_metadata_store,
    make_security_context,
)
from tests.integration.test_e007_end_to_end import _corpus, _ingest


def _mgr(mstore, vstore, pstore) -> DocumentManager:
    return DocumentManager(
        metadata_store=mstore,
        vector_store=vstore,
        parent_store=pstore,
        chunker=ParentChildChunker(),
        dense_encoder=FakeDenseEncoder(),
        sparse_encoder=FakeSparseEncoder(),
    )


def _retriever(vstore, pstore, mstore) -> SecureRetriever:
    return SecureRetriever(
        _HybridSearchAdapter(vstore), ParentReader(pstore), metadata_store=mstore
    )


def test_lifecycle_ingest_retrieve_update_delete_purge_reingest() -> None:
    acl = acl_payload(tenant_id="t1", acl_scope="tenant", security_level="public")
    vstore, pstore, _ = _ingest("eng", "t1", acl)
    mstore = active_metadata_store("t1", "eng", "doc1", "v1")
    mgr = _mgr(mstore, vstore, pstore)
    ctx = make_security_context()
    retriever = _retriever(vstore, pstore, mstore)

    # ingest -> retrieve (v1 present)
    res = retriever.retrieve(
        ctx, "architecture planner security", _corpus(), dense_encoder=FakeDenseEncoder(), sparse_encoder=FakeSparseEncoder()
    )
    assert res.hits
    assert all(v == "v1" for _, h in res.hits for v in [h.document_version])

    # update -> new active version, old deprecated
    mgr.update(
        ctx, corpus_id="eng", document_id="doc1",
        content="updated content for the system", job_id="j-upd", document_version="v2",
        acl=ResourceAcl(**acl),
    )
    res2 = retriever.retrieve(
        ctx, "architecture planner security", _corpus(), dense_encoder=FakeDenseEncoder(), sparse_encoder=FakeSparseEncoder()
    )
    assert res2.hits
    assert all(h.document_version == "v2" for _, h in res2.hits)

    # logical delete -> immediate retrieval filter (no background purge needed)
    mgr.delete(ctx, corpus_id="eng", document_id="doc1")
    res3 = retriever.retrieve(
        ctx, "anything", _corpus(), dense_encoder=FakeDenseEncoder(), sparse_encoder=FakeSparseEncoder()
    )
    assert res3.hits == []

    # physical purge -> control plane gone
    mgr.purge(ctx, corpus_id="eng", document_id="doc1")
    assert mstore.get_document_latest("t1", "eng", "doc1") is None

    # re-ingest fresh on the same stores
    mgr.ingest(
        IngestionRequest(
            tenant_id="t1", corpus_id="eng", document_id="doc1",
            document_version="v3", content="brand new content for the system",
            acl=ResourceAcl(**acl), job_id="j-re",
        )
    )
    res4 = retriever.retrieve(
        ctx, "anything", _corpus(), dense_encoder=FakeDenseEncoder(), sparse_encoder=FakeSparseEncoder()
    )
    assert res4.hits
    assert all(h.document_version == "v3" for _, h in res4.hits)
