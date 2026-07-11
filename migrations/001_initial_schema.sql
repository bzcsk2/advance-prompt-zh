-- Initial Metadata Store Schema
-- SQLite-compatible. Run against metadata.db during bootstrap.
--
-- Migration: 001_initial_schema
-- Applied:   by bootstrap command (not during uv sync)

CREATE TABLE IF NOT EXISTS corpus_registry (
    corpus_id           TEXT NOT NULL,
    tenant_id           TEXT NOT NULL,
    name                TEXT NOT NULL,
    description         TEXT NOT NULL DEFAULT '',
    domain              TEXT NOT NULL DEFAULT '',
    owner               TEXT NOT NULL DEFAULT '',
    source_type         TEXT NOT NULL DEFAULT 'documents',
    vector_collection   TEXT,
    parent_store_namespace TEXT,
    enabled             INTEGER NOT NULL DEFAULT 1,
    searchable          INTEGER NOT NULL DEFAULT 1,
    authority_level     INTEGER NOT NULL DEFAULT 50,
    freshness_sla_hours INTEGER,
    security_policy_id  TEXT NOT NULL DEFAULT 'default',
    default_security_level TEXT NOT NULL DEFAULT 'internal',
    capability_ids      TEXT NOT NULL DEFAULT '[]',
    metadata_schema     TEXT NOT NULL DEFAULT '{}',
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    PRIMARY KEY (corpus_id, tenant_id),
    CHECK (authority_level >= 0 AND authority_level <= 100)
);

CREATE TABLE IF NOT EXISTS documents (
    document_id         TEXT NOT NULL,
    tenant_id           TEXT NOT NULL,
    corpus_id           TEXT NOT NULL,
    source_uri          TEXT NOT NULL,
    source_connector    TEXT NOT NULL DEFAULT 'file',
    source_native_id    TEXT,
    title               TEXT NOT NULL DEFAULT '',
    source_filename     TEXT NOT NULL DEFAULT '',
    mime_type           TEXT NOT NULL DEFAULT 'text/plain',
    version             TEXT NOT NULL,
    content_hash        TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'discovered',
    effective_from      TEXT,
    effective_to        TEXT,
    authority_level     INTEGER NOT NULL DEFAULT 50,
    deprecated          INTEGER NOT NULL DEFAULT 0,
    supersedes_document_id TEXT,
    acl_policy_id       TEXT NOT NULL DEFAULT 'default',
    security_level      TEXT NOT NULL DEFAULT 'internal',
    acl_scope           TEXT NOT NULL DEFAULT 'restricted',
    allowed_user_ids    TEXT NOT NULL DEFAULT '[]',
    allowed_group_ids   TEXT NOT NULL DEFAULT '[]',
    denied_user_ids     TEXT NOT NULL DEFAULT '[]',
    denied_group_ids    TEXT NOT NULL DEFAULT '[]',
    parser_name         TEXT NOT NULL DEFAULT '',
    parser_version      TEXT NOT NULL DEFAULT '',
    chunking_version    TEXT NOT NULL DEFAULT '',
    embedding_model     TEXT NOT NULL DEFAULT '',
    embedding_version   TEXT NOT NULL DEFAULT '',
    discovered_at       TEXT NOT NULL,
    indexed_at          TEXT,
    deleted_at          TEXT,
    last_synced_at      TEXT NOT NULL,
    PRIMARY KEY (document_id, tenant_id, corpus_id, version),
    FOREIGN KEY (corpus_id, tenant_id)
        REFERENCES corpus_registry(corpus_id, tenant_id),
    CHECK (authority_level >= 0 AND authority_level <= 100),
    CHECK (acl_scope IN ('tenant', 'restricted')),
    CHECK (status IN ('discovered', 'processing', 'active', 'failed', 'deprecated', 'deleted')),
    CHECK (
        (status = 'active' AND indexed_at IS NOT NULL) OR
        (status != 'active')
    ),
    CHECK (
        (status = 'deleted' AND deleted_at IS NOT NULL) OR
        (status != 'deleted')
    )
);

CREATE TABLE IF NOT EXISTS ingestion_jobs (
    job_id              TEXT NOT NULL PRIMARY KEY,
    document_id         TEXT NOT NULL,
    document_version    TEXT NOT NULL,
    corpus_id           TEXT NOT NULL,
    tenant_id           TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'queued',
    started_at          TEXT NOT NULL,
    finished_at         TEXT,
    raw_hash            TEXT NOT NULL DEFAULT '',
    parsed_hash         TEXT,
    parent_count        INTEGER NOT NULL DEFAULT 0,
    child_count         INTEGER NOT NULL DEFAULT 0,
    parser_version      TEXT NOT NULL DEFAULT '',
    chunking_version    TEXT NOT NULL DEFAULT '',
    embedding_version   TEXT NOT NULL DEFAULT '',
    error_code          TEXT,
    error_message       TEXT,
    FOREIGN KEY (document_id, tenant_id, corpus_id, document_version)
        REFERENCES documents(document_id, tenant_id, corpus_id, version),
    CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'cancelling', 'cancelled'))
);

CREATE TABLE IF NOT EXISTS chunks (
    chunk_id            TEXT NOT NULL PRIMARY KEY,
    tenant_id           TEXT NOT NULL,
    corpus_id           TEXT NOT NULL,
    document_id         TEXT NOT NULL,
    document_version    TEXT NOT NULL,
    parent_id           TEXT,
    chunk_type          TEXT NOT NULL CHECK (chunk_type IN ('parent', 'child')),
    page_number         INTEGER,
    section_path        TEXT NOT NULL DEFAULT '[]',
    start_offset        INTEGER,
    end_offset          INTEGER,
    content             TEXT NOT NULL,
    content_hash        TEXT NOT NULL,
    effective_from      TEXT,
    effective_to        TEXT,
    authority_level     INTEGER NOT NULL DEFAULT 50,
    deprecated          INTEGER NOT NULL DEFAULT 0,
    acl_policy_id       TEXT NOT NULL DEFAULT 'default',
    security_level      TEXT NOT NULL DEFAULT 'internal',
    acl_scope           TEXT NOT NULL DEFAULT 'restricted',
    allowed_user_ids    TEXT NOT NULL DEFAULT '[]',
    allowed_group_ids   TEXT NOT NULL DEFAULT '[]',
    denied_user_ids     TEXT NOT NULL DEFAULT '[]',
    denied_group_ids    TEXT NOT NULL DEFAULT '[]',
    metadata            TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (document_id, tenant_id, corpus_id, document_version)
        REFERENCES documents(document_id, tenant_id, corpus_id, version),
    CHECK (authority_level >= 0 AND authority_level <= 100),
    CHECK (acl_scope IN ('tenant', 'restricted')),
    CHECK (start_offset >= 0 OR start_offset IS NULL),
    CHECK (end_offset >= 0 OR end_offset IS NULL),
    CHECK (
        (start_offset IS NULL AND end_offset IS NULL) OR
        (start_offset IS NOT NULL AND end_offset IS NOT NULL AND end_offset >= start_offset)
    )
);

CREATE INDEX IF NOT EXISTS idx_documents_tenant_corpus
    ON documents(tenant_id, corpus_id);

CREATE INDEX IF NOT EXISTS idx_documents_corpus_status
    ON documents(corpus_id, status);

CREATE INDEX IF NOT EXISTS idx_ingestion_jobs_tenant
    ON ingestion_jobs(tenant_id);

CREATE INDEX IF NOT EXISTS idx_ingestion_jobs_document
    ON ingestion_jobs(document_id, document_version);

CREATE INDEX IF NOT EXISTS idx_chunks_tenant
    ON chunks(tenant_id);

CREATE UNIQUE INDEX IF NOT EXISTS idx_documents_active_version
    ON documents(tenant_id, corpus_id, document_id)
    WHERE status = 'active';
