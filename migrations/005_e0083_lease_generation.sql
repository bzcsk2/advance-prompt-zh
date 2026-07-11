-- E-008.3 migration: fencing token for the build lease (P1-2).
-- Each claim / takeover / resume of a build lease increments lease_generation
-- so a stale owner (whose lease was taken over by a concurrent delivery) is
-- rejected (BuildConflict) before it can mutate the shared data plane. Applied
-- by the MetadataStore migrator; safe to re-apply (existing rows default to 0).

ALTER TABLE document_builds ADD COLUMN lease_generation INTEGER NOT NULL DEFAULT 0;
