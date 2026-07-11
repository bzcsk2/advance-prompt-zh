-- E-008.2 migration: atomic build lease (P1-3 / P1-4).
-- A single (tenant, corpus, document, document_version) is owned by exactly one
-- in-flight build at a time. acquire_job claims the lease atomically, so a
-- concurrent in-flight build for the same artifact is rejected with
-- BuildConflict instead of racing on the shared data plane (deterministic IDs).
-- A terminal owner's build can be taken over by a re-delivered job.
-- Applied by the MetadataStore migrator; safe to re-apply.

CREATE TABLE IF NOT EXISTS document_builds (
    tenant_id       TEXT NOT NULL,
    corpus_id       TEXT NOT NULL,
    document_id     TEXT NOT NULL,
    document_version TEXT NOT NULL,
    owner_job_id    TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'running',
    base_revision   INTEGER NOT NULL DEFAULT 0,
    acquired_at     TEXT NOT NULL,
    PRIMARY KEY (tenant_id, corpus_id, document_id, document_version),
    CHECK (status IN ('running', 'done', 'failed'))
);
