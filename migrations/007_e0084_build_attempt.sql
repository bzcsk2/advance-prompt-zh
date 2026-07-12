-- E-008.4 migration: persist the execution attempt on the build lease (P1-3).
-- A same-job_id re-delivery is now distinguishable from a genuine
-- recovery at the DATABASE level, not just in-process memory. Each
-- acquire/takeover/recovery mints a fresh attempt_id and refreshes
-- claimed_at; a second live attempt for the same (job_id, RUNNING
-- lease) is rejected (BuildConflict) instead of being treated as a
-- same-owner resume that races on deterministic point IDs. Recovery
-- of a terminal lease advances the attempt_id. A lease timeout /
-- heartbeat (out of scope for M1) is the production-grade completion
-- of cross-process liveness; this column is the data model it needs.
-- See docs/issue-e0084-contract.md P1-3.

ALTER TABLE document_builds ADD COLUMN attempt_id TEXT;
ALTER TABLE document_builds ADD COLUMN claimed_at TEXT;
