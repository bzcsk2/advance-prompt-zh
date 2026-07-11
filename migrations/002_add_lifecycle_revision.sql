-- E-008 migration: monotonic lifecycle_revision + reentrant job step markers.
-- Build plan §10.10 #3 (step markers), #8 (monotonic lifecycle_revision for
-- delete/update competition ordering). Applied by the MetadataStore migrator.
--
-- Safe to re-apply: ADD COLUMN is guarded by the schema_migrations table; the
-- job_steps DDL uses IF NOT EXISTS.

ALTER TABLE documents ADD COLUMN lifecycle_revision INTEGER NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS job_steps (
    job_id      TEXT NOT NULL,
    step_name   TEXT NOT NULL,
    status      TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (job_id, step_name),
    FOREIGN KEY (job_id) REFERENCES ingestion_jobs(job_id)
);

CREATE INDEX IF NOT EXISTS idx_job_steps_job ON job_steps(job_id);
