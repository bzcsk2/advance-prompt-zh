-- Per-corpus index-switch lease (E-022 contract P1-3).
-- At most one in-flight index switch (build -> pointer flip, or rollback) per
-- corpus at a time. Serializes switch_index / rollback_index so a concurrent
-- switch cannot leave the persisted pointer and the live in-memory registry
-- inconsistent. The lease owner is the switch operation; it is fenced so only
-- the holder may flip the pointer inside the BEGIN IMMEDIATE transaction.
-- Applied by MetadataStore.apply_migrations(); safe to re-apply.

CREATE TABLE IF NOT EXISTS index_switch_leases (
    corpus_id   TEXT PRIMARY KEY,
    owner       TEXT NOT NULL,
    acquired_at TEXT NOT NULL
);
