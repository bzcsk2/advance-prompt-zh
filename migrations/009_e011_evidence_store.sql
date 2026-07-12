-- Evidence snapshot store (E-011, build plan §12.8)
-- Applied by EvidenceSnapshotStore.apply_migrations() at construction time.

CREATE TABLE IF NOT EXISTS evidence_snapshots (
    evidence_id        TEXT PRIMARY KEY,
    tenant_id          TEXT NOT NULL,
    corpus_id          TEXT NOT NULL,
    document_id        TEXT NOT NULL,
    document_version   TEXT NOT NULL,
    source_uri         TEXT NOT NULL DEFAULT '',
    source_filename    TEXT NOT NULL DEFAULT '',
    parent_id          TEXT,
    child_chunk_id     TEXT,
    page_number        INTEGER,
    section_path       TEXT NOT NULL DEFAULT '[]',
    start_offset       INTEGER,
    end_offset         INTEGER,
    text               TEXT NOT NULL,
    text_hash          TEXT NOT NULL,
    retrieval_query    TEXT NOT NULL DEFAULT '',
    retrieval_score   REAL,
    rerank_score       REAL,
    authority_level    INTEGER NOT NULL DEFAULT 50,
    effective_from     TEXT,
    effective_to       TEXT,
    deprecated         INTEGER NOT NULL DEFAULT 0,
    retrieved_at       TEXT NOT NULL,
    acl_policy_id      TEXT NOT NULL DEFAULT 'unknown',
    policy_version     TEXT NOT NULL,
    retrieval_iteration INTEGER NOT NULL DEFAULT 0,
    plan_step_id       TEXT,
    security_level     TEXT NOT NULL DEFAULT 'internal',
    acl_scope          TEXT NOT NULL DEFAULT 'restricted',
    allowed_user_ids   TEXT NOT NULL DEFAULT '[]',
    allowed_group_ids  TEXT NOT NULL DEFAULT '[]',
    denied_user_ids    TEXT NOT NULL DEFAULT '[]',
    denied_group_ids   TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS evidence_audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    evidence_id TEXT NOT NULL,
    tenant_id   TEXT NOT NULL,
    actor_user_id TEXT NOT NULL,
    action      TEXT NOT NULL,
    reason      TEXT NOT NULL DEFAULT '',
    at          TEXT NOT NULL
);
