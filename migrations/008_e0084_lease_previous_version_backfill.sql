-- E-008.4 closure (P1-2 upgrade path): one-time backfill of
-- document_builds.previous_active_version.
--
-- Migration 006 (already published in the prior closure commit) only ADDED the
-- nullable column. A database that had already deployed 006 recorded that
-- migration version as applied, so the migrator skips it on later upgrades and
-- any backfill placed inside 006 would never execute. This SEPARATE migration
-- version runs the backfill for those already-upgraded databases.
--
-- It copies the per-job previous_active_version onto the (already-claimed) lease
-- each build owns, so a post-commit-failed build taken over after upgrade still
-- inherits the true replaced version instead of recomputing against the
-- already-switched active version (and failing to clean the prior data plane).
--
-- Idempotent: only fills NULL lease rows. Safe on a fresh database (no rows) and
-- on rows already populated by acquire_job (non-NULL -> untouched).
--
-- Applied atomically with its schema_migrations marker by apply_migrations.

UPDATE document_builds
SET previous_active_version = (
    SELECT ingestion_jobs.previous_active_version
    FROM ingestion_jobs
    WHERE ingestion_jobs.job_id = document_builds.owner_job_id
)
WHERE previous_active_version IS NULL;
