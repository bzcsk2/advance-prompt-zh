"""End-to-end E-007 retrieval: chunk -> upsert -> hybrid -> parent 2nd auth.

Validates the full secure flow with the real chunker feeding the real Qdrant
store and parent store, including tenant isolation and corpus discoverability.
"""

from datetime import datetime

from qdrant_client import QdrantClient

from agentic_rag_enterprise.domain.corpus import CorpusConfig
from agentic_rag_enterprise.ingestion.chunker import ParentChildChunker, ParentChunk
from agentic_rag_enterprise.retrieval.hybrid import _HybridSearchAdapter
from agentic_rag_enterprise.retrieval.parent_reader import ParentReader
from agentic_rag_enterprise.retrieval.retriever import SecureRetriever
from agentic_rag_enterprise.security.policy import ResourceAcl
from agentic_rag_enterprise.storage.parent_store import ParentStore
from agentic_rag_enterprise.storage.vector_store import VectorStore, child_chunk_to_point
from tests.fixtures import (
    DENSE_DIM,
    FakeDenseEncoder,
    FakeSparseEncoder,
    SAMPLE_MARKDOWN,
    acl_payload,
    active_metadata_store,
    make_security_context,
)


def _corpus(corpus_id: str = "eng", tenant_id: str = "t1", **kw) -> CorpusConfig:
    base: dict = dict(
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
        created_at=datetime.now(),
        updated_at=datetime.now(),
    )
    base.update(kw)
    return CorpusConfig(**base)


def _ingest(corpus_id: str, tenant_id: str, acl: dict) -> tuple[VectorStore, ParentStore, list]:
    chunker = ParentChildChunker()
    parents, children = chunker.chunk_markdown(
        SAMPLE_MARKDOWN,
        tenant_id=tenant_id,
        corpus_id=corpus_id,
        document_id="doc1",
        document_version="v1",
    )

    client = QdrantClient(location=":memory:")
    store = VectorStore(client)
    store.create_collection(corpus_id, dense_size=DENSE_DIM)

    # Real production mapper: chunker output -> Qdrant PointStruct. The point
    # carries the full ACL so retrieval can re-establish authorization at read
    # time. The point id is a stable UUID derived from the content-addressed
    # child id.
    resource_acl = ResourceAcl(**acl)
    points = [
        child_chunk_to_point(
            child,
            resource_acl,
            status="active",
            deprecated=False,
            dense_encoder=FakeDenseEncoder(),
            sparse_encoder=FakeSparseEncoder(),
        )
        for child in children
    ]
    store.upsert(corpus_id, points)

    # The parent store is raw/untrusted; it must carry the full authorization
    # metadata set (lifecycle + ACL) so the parent second-authorization pass
    # can validate it fail-closed. We keep the chunker-derived parent_id and
    # version, and only *supplement* the ACL metadata.
    pstore = ParentStore()
    auth_metadata = {
        "status": "active",
        "deprecated": False,
        "security_level": acl["security_level"],
        "acl_scope": acl["acl_scope"],
        "allowed_user_ids": acl["allowed_user_ids"],
        "allowed_group_ids": acl["allowed_group_ids"],
        "denied_user_ids": acl["denied_user_ids"],
        "denied_group_ids": acl["denied_group_ids"],
    }
    for parent in parents:
        pstore.put(
            ParentChunk(
                parent_id=parent.parent_id,
                document_id=parent.document_id,
                document_version=parent.document_version,
                tenant_id=parent.tenant_id,
                corpus_id=parent.corpus_id,
                text=parent.text,
                section_path=parent.section_path,
                metadata={**parent.metadata, **auth_metadata},
            )
        )
    return store, pstore, children


def test_end_to_end_returns_authorized_parents() -> None:
    acl = acl_payload(tenant_id="t1", acl_scope="tenant", security_level="public")
    store, pstore, children = _ingest("eng", "t1", acl)
    retriever = SecureRetriever(
        _HybridSearchAdapter(store),
        ParentReader(pstore),
        metadata_store=active_metadata_store("t1", "eng", "doc1", "v1"),
    )

    result = retriever.retrieve(
        make_security_context(),
        "architecture planner security",
        _corpus(),
        dense_encoder=FakeDenseEncoder(),
        sparse_encoder=FakeSparseEncoder(),
    )
    assert result.hits
    assert result.denied_parent_count == 0
    for hit, parent in result.hits:
        assert parent.parent_id == hit.parent_id
        assert parent.content
        assert parent.document_version == "v1"


def test_tenant_isolation_end_to_end() -> None:
    acl = acl_payload(tenant_id="t1", acl_scope="tenant", security_level="public")
    store, pstore, _ = _ingest("eng", "t1", acl)
    retriever = SecureRetriever(
        _HybridSearchAdapter(store),
        ParentReader(pstore),
        metadata_store=active_metadata_store("t1", "eng", "doc1", "v1"),
    )

    # A user from a different tenant gets no hits (filter-less retrieval blocked).
    result = retriever.retrieve(
        make_security_context(tenant_id="t2"),
        "anything",
        _corpus(),
        dense_encoder=FakeDenseEncoder(),
        sparse_encoder=FakeSparseEncoder(),
    )
    assert result.hits == []


def test_corpus_discoverability_end_to_end() -> None:
    acl = acl_payload(tenant_id="t1", acl_scope="tenant", security_level="public")
    store, pstore, _ = _ingest("eng", "t1", acl)
    retriever = SecureRetriever(
        _HybridSearchAdapter(store),
        ParentReader(pstore),
        metadata_store=active_metadata_store("t1", "eng", "doc1", "v1"),
    )

    ctx = make_security_context(allowed_corpus_ids=["other_corpus"])
    result = retriever.retrieve(
        ctx,
        "anything",
        _corpus(),
        dense_encoder=FakeDenseEncoder(),
        sparse_encoder=FakeSparseEncoder(),
    )
    assert result.hits == []


def test_disabled_corpus_blocks() -> None:
    acl = acl_payload(tenant_id="t1", acl_scope="tenant", security_level="public")
    store, pstore, _ = _ingest("eng", "t1", acl)
    retriever = SecureRetriever(
        _HybridSearchAdapter(store),
        ParentReader(pstore),
        metadata_store=active_metadata_store("t1", "eng", "doc1", "v1"),
    )

    result = retriever.retrieve(
        make_security_context(),
        "anything",
        _corpus(enabled=False),
        dense_encoder=FakeDenseEncoder(),
        sparse_encoder=FakeSparseEncoder(),
    )
    assert result.hits == []
