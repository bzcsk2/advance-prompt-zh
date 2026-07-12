-- E-008.4 migration: bind previous_active_version to the BUILD LEASE (P1-2).
-- Previously this column lived only on ingestion_jobs, so a replacement job
-- taking over the lease recomputed it against the (already-switched) active
-- version and failed to clean the truly-replaced data plane. Binding it to the
-- lease -- captured at the FIRST claim, carried forward on takeover/resume --
-- keeps "the version this build replaces" stable across the whole build
-- identity (see docs/issue-e0084-contract.md P1-2).

ALTER TABLE document_builds ADD COLUMN previous_active_version TEXT;

-- One-time backfill for E-008.3 databases upgraded in place: copy the
-- per-job previous_active_version onto the (already-claimed) lease it owns,
-- so a post-commit-failed build taken over after upgrade still inherits the
-- true replaced version instead of recomputing against the switched active
-- version. Idempotent: only fills NULL lease rows.
UPDATE document_builds
SET previous_active_version = (
    SELECT ingestion_jobs.previous_active_version
    FROM ingestion_jobs
    WHERE ingestion_jobs.job_id = document_builds.owner_job_id
)
WHERE previous_active_version IS NULL;
