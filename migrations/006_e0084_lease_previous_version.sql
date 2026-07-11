-- E-008.4 migration: bind previous_active_version to the BUILD LEASE (P1-2).
-- Previously this column lived only on ingestion_jobs, so a replacement job
-- taking over the lease recomputed it against the (already-switched) active
-- version and failed to clean the truly-replaced data plane. Binding it to the
-- lease -- captured at the FIRST claim, carried forward on takeover/resume --
-- keeps "the version this build replaces" stable across the whole build
-- identity (see docs/issue-e0084-contract.md P1-2).

ALTER TABLE document_builds ADD COLUMN previous_active_version TEXT;
