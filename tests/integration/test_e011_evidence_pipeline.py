"""End-to-end E-011: retrieval -> deduplication -> Evidence snapshot store.

Validates the full secure flow persists immutable Evidence snapshots and that
deduplication collapses overlapping hits (build plan §12.4 / §12.6 / §12.8),
using the same real Qdrant + parent store harness as the E-007 tests.
"""

import os
import tempfile
from datetime import datetime

from qdrant_client import QdrantClient

from agentic_rag_enterprise.domain.corpus import CorpusConfig
from agentic_rag_enterprise.domain.evidence import Evidence
from agentic_rag_enterprise.ingestion.chunker import ParentChildChunker, ParentChunk
from agentic_rag_enterprise.retrieval.hybrid import _HybridSearchAdapter
from agentic_rag_enterprise.retrieval.parent_reader import ParentReader
from agentic_rag_enterprise.retrieval.retriever import SecureRetriever
from agentic_rag_enterprise.retrieval.models import RetrievalResult
from agentic_rag_enterprise.security.policy import ResourceAcl
from agentic_rag_enterprise.storage.evidence_store import (
    EvidenceAccessLevel,
    EvidenceSnapshotStore,
)
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


def _ingest(corpus_id: str, tenant_id: str, acl: dict, content: str = SAMPLE_MARKDOWN):
    chunker = ParentChildChunker()
    parents, children = chunker.chunk_markdown(
        content,
        tenant_id=tenant_id,
        corpus_id=corpus_id,
        document_id="doc1",
        document_version="v1",
    )
    client = QdrantClient(location=":memory:")
    store = VectorStore(client)
    store.create_collection(corpus_id, dense_size=DENSE_DIM)

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
    return store, pstore, parents, children


def _evidence_store() -> EvidenceSnapshotStore:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    return EvidenceSnapshotStore(path)


def test_retrieve_evidence_persists_snapshots() -> None:
    acl = acl_payload(tenant_id="t1", acl_scope="tenant", security_level="public")
    store, pstore, _parents, _children = _ingest("eng", "t1", acl)

    fd, ev_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(ev_path)
    evidence_store = EvidenceSnapshotStore(ev_path)

    retriever = SecureRetriever(
        _HybridSearchAdapter(store),
        ParentReader(pstore),
        metadata_store=active_metadata_store("t1", "eng", "doc1", "v1"),
        evidence_store=evidence_store,
    )

    evidence = retriever.retrieve_evidence(
        make_security_context(),
        "architecture planner security",
        _corpus(),
        dense_encoder=FakeDenseEncoder(),
        sparse_encoder=FakeSparseEncoder(),
    )

    assert evidence, "expected at least one evidence snapshot"
    assert all(isinstance(e, Evidence) for e in evidence)
    # Persisted exactly the snapshots returned.
    assert evidence_store.count("t1") == len(evidence)
    for ev in evidence:
        assert ev.text  # immutable body captured
        assert ev.source_uri == "inline://doc1"
        assert ev.source_filename == "doc1.md"
        assert ev.authority_level == 50
        assert ev.acl_policy_id == "default"
        assert ev.retrieval_iteration == 0
        # Re-readable under the same principal at full access.
        access = evidence_store.get(ev.evidence_id, make_security_context())
        assert access.level is EvidenceAccessLevel.FULL


def test_deduplication_collapses_same_parent_children() -> None:
    # A long repeated section splits into multiple children under ONE parent;
    # a query matching that phrase surfaces several child hits that must collapse
    # to a single Evidence snapshot (same-parent dedup, build plan §12.6 #2).
    phrase = "architecture planner security query routing corpus "
    # One section sized between min/max parent bounds -> a SINGLE parent that
    # the child splitter divides into several children sharing one parent_id.
    long_content = "# System Overview\n\n" + phrase * 60
    acl = acl_payload(tenant_id="t1", acl_scope="tenant", security_level="public")
    store, pstore, _parents, children = _ingest("eng", "t1", acl, content=long_content)
    assert len(children) > 1, "fixture must produce multiple children to test dedup"

    retriever = SecureRetriever(
        _HybridSearchAdapter(store),
        ParentReader(pstore),
        metadata_store=active_metadata_store("t1", "eng", "doc1", "v1"),
        evidence_store=_evidence_store(),
    )

    raw: RetrievalResult = retriever.retrieve(
        make_security_context(),
        "architecture planner security query routing corpus",
        _corpus(),
        dense_encoder=FakeDenseEncoder(),
        sparse_encoder=FakeSparseEncoder(),
    )
    evidence = retriever.retrieve_evidence(
        make_security_context(),
        "architecture planner security query routing corpus",
        _corpus(),
        dense_encoder=FakeDenseEncoder(),
        sparse_encoder=FakeSparseEncoder(),
    )

    # The phrase matches several children of the same parent in the raw hits...
    assert len(raw.hits) > 1
    # ...but deduplication collapses them to a single Evidence snapshot.
    assert len(evidence) == 1
    # No two surviving evidence share the same parent (same-parent rule).
    parent_ids = [e.parent_id for e in evidence]
    assert len(parent_ids) == len(set(parent_ids))
