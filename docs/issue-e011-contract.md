# E-011 Issue Contract (M2) — Evidence snapshot store and deduplication

First capability of Milestone 2 (Evidence, Fast Path, Grounded Answer, build
plan §3532). Extends the E-007/E-009 retrieval chain so that, before a claim is
cited, each surviving hit is (a) deduplicated against its siblings by span,
same-parent, and near-duplicate text, and (b) frozen into an immutable
`Evidence` snapshot that is independently re-authorized at read time. This is
the "snapshot, not a link" model from §12.8: the body, source metadata, scores,
and the policy version in force at write time are all stored immutably, so a
later ACL tightening / document revocation cannot silently alter what the model
was grounded on.

Baseline `3115fc00` is the branch point. E-011 is additive to `retrieval/` and
`storage/` — it does NOT change the existing `retrieve()` path, so E-007/E-009
behaviour and tests remain green. Scope is a strict subset of M2 paths:
`retrieval/deduplication.py` (new), `retrieval/evidence.py` (new),
`storage/evidence_store.py` (new), `retrieval/retriever.py` (additive
`retrieve_evidence`), `retrieval/__init__.py`, `storage/__init__.py`,
`domain/security.py` (capability `permissions` field), `tests/...`, `docs/`,
`migrations/`.

## Goal
Provide an `Evidence` snapshot store plus a retrieval-time deduplicator so that
the answer pipeline receives a stable, deduplicated, re-authorizable set of
evidence. `SecureRetriever.retrieve_evidence(...)` returns
`list[domain.evidence.Evidence]` (the immutable snapshot model, NOT the M0 mock
`schemas.Evidence`), persisting each snapshot to the evidence store and
collapsing redundant hits before they are cited.

## depends_on
- **E-008** — idempotent `IngestionJob` + active-version protocol + Metadata DB
  control plane. E-011 reads `MetadataStore.get_active_document` for the
  `SourceDocument` source metadata of a snapshot.
- **E-009** — parent-store secondary authorization (`ParentReader`). E-011
  reuses the already-authorized `AuthorizedParent` returned by `retrieve()` as
  the snapshot body source and the ACL summary for write-time save.
- **build plan §12.4** — pipeline order: normalize → deduplicate → rerank →
  authority/freshness → snapshot. E-011 owns the `deduplicate` and `snapshot`
  stages (rerank/authority/freshness are later M2 issues).
- **build plan §12.6** — three deduplication dimensions: exact span,
  same-parent multi-child, near-duplicate text. E-011 implements all three as
  sequential collapse passes.
- **build plan §12.8** — Evidence Store: immutable snapshot, not a document
  link; stores body + source metadata + scores + `policy_version` + source ACL
  summary; read re-authorized by the current principal; `audit:evidence:read`
  auditors produce audit events.

## Non-goals (M2 only)
- No reranker / authority / freshness stages (later M2 issues) — dedup runs on
  the raw `retrieve()` hits and their `authority_level`/`score` only.
- No claim-citation or grounded-answer composition (later M2 issues).
- No new dependencies; the evidence store is SQLite-backed like `MetadataStore`.
- No mutation of the existing `retrieve()` behaviour or its callers.
- No cross-tenant evidence sharing; cross-tenant reads are `DENIED` (fail-closed).

## Design decisions (RECOMMENDED defaults — confirmed during implementation)
1. **Two `Evidence` models, used for distinct purposes.** `domain/evidence.py`
   is the M2 immutable snapshot (frozen pydantic, `evidence_id`, `text_hash`,
   provenance, `policy_version`) and is what `retrieve_evidence` returns and
   what the store persists. `schemas.Evidence` is the M0 baseline mock and is
   used ONLY by the M0 `Retriever`. No code path returns `schemas.Evidence` from
   the M2 pipeline.
2. **Deduplication is three sequential collapse passes** in
   `Deduplicator.deduplicate`:
   - `_collapse` by exact span `(document_id, document_version, chunk_id)`;
   - `_collapse` by `parent_id` (same parent, multiple child hits);
   - `_collapse_text` cross-corpus near-duplicate on `normalize_text(text)`
     (lowercase + whitespace collapse). When two candidates tie on normalized
     text, `_is_better` keeps the higher `authority_level`, then the higher
     `score`. `_fold` merges the loser's `RetrievalContext`s into
     `duplicate_sources` on the survivor.
3. **Evidence is built from the authorized parent, not the raw hit.**
   `EvidenceBuilder.build/build_from_candidate` prefers `parent.content` (the
   full authorized parent body) falling back to `hit.text`. Provenance
   (`SourceDocument` fields), `text_hash` (sha256), and `policy_version` come
   from the `SecurityContext`. This keeps the "snapshot, not a link" guarantee:
   the stored body never re-reads the document at read time.
4. **Write-time save is idempotent** (`INSERT OR IGNORE` keyed on `evidence_id`)
   and stores a source `ResourceAcl` summary so read-time re-auth has the ACL
   that was in force at write time.
5. **Read-time re-authorization levels: FULL / REDACTED / DENIED.**
   `EvidenceSnapshotStore.get(evidence_id, ctx)`:
   - cross-tenant → `DENIED` (no body);
   - same tenant AND `can_discover_corpus` → `FULL`;
   - else if `audit:evidence:read` in `ctx.permissions` → `FULL` + an audit
     event via `audit_callback`;
   - else → `REDACTED` (body withheld via `_redact_evidence`, metadata retained).
   Authorization is fail-closed: any missing principal field → `DENIED`.
6. **Fine-grained `audit:evidence:read` is a capability, not a role.**
   `SecurityContext.permissions: list[str]` was added so an auditor can be
   granted evidence read without implying `is_admin` blanket access
   (build plan §12.8). It is never model-supplied.
7. **No circular imports.** `evidence_store` must not import
   `retrieval.models.as_acl_scope`; an inlined `_as_acl_scope(value) ->
   Literal["tenant","restricted"]` coercion is used instead, because
   `retrieval/__init__` re-exports `retriever` which imports `evidence_store`.
8. **Architecture boundary preserved.** `retrieve_evidence` builds an
   `owners` dict and uses `owners.get(key)` (dict subscript), never a direct
   `.get` on a parent-named binding, satisfying `tests/retrieval/
   test_retrieval_boundary.py` (no implicit DB/ACL re-read on a parent object).

## Allowed paths (M2 only)
- `src/agentic_rag_enterprise/retrieval/deduplication.py` (new) — `normalize_text`,
  `RetrievalContext`, `DedupCandidate`, `Deduplicator`.
- `src/agentic_rag_enterprise/retrieval/evidence.py` (new) — `EvidenceBuilder`.
- `src/agentic_rag_enterprise/storage/evidence_store.py` (new) —
  `EvidenceSnapshotStore`, `EvidenceAccess`, `EvidenceAccessLevel`.
- `migrations/009_e011_evidence_store.sql` (new) — `evidence_snapshots` +
  `evidence_audit_log` DDL, matching `apply_migrations`.
- `src/agentic_rag_enterprise/retrieval/retriever.py` — add
  `retrieve_evidence(...)` (additive); `SecureRetriever.__init__` gains
  `evidence_store`, `deduplicator`, `evidence_builder` (all optional, backward
  compatible). `retrieve()` UNCHANGED.
- `src/agentic_rag_enterprise/retrieval/__init__.py` — export `Deduplicator`,
  `EvidenceBuilder`.
- `src/agentic_rag_enterprise/storage/__init__.py` — export `EvidenceSnapshotStore`.
- `src/agentic_rag_enterprise/domain/security.py` — add
  `SecurityContext.permissions: list[str]`.
- `tests/unit/test_deduplication.py`, `tests/unit/test_evidence_store.py`,
  `tests/integration/test_e011_evidence_pipeline.py` (new).
- `docs/issue-e011-contract.md` (this file).

## Forbidden
- No change to `retrieve()` behaviour or its existing callers.
- No return of `schemas.Evidence` from the M2 pipeline.
- No new third-party dependencies; SQLite only.
- No direct `.get` on a parent-named binding in `retrieve_evidence` (boundary).
- No silent granting of evidence bodies: fail-closed; cross-tenant reads are
  `DENIED`; non-discoverable reads are `REDACTED` unless `audit:evidence:read`.
- No import of `retrieval.models` from `storage/evidence_store.py` (circular).
- MUST NOT mutate a stored snapshot (immutable); `save` is `INSERT OR IGNORE`.

## Acceptance tests
- `tests/unit/test_deduplication.py` — exact-span collapse; same-parent
  multi-child collapse; near-duplicate text collapse (case/space only);
  cross-corpus keeps higher `authority_level` then higher `score`; stable
  ordering; empty input → `[]`; context folding records `duplicate_sources`.
- `tests/unit/test_evidence_store.py` — `save` then `get` returns FULL for the
  owning principal; `save` is idempotent (`INSERT OR IGNORE`); cross-tenant
  `get` → `DENIED`; non-discoverable same-tenant with no audit grant →
  `REDACTED` (body withheld); `audit:evidence:read` permission → FULL + audit
  event; snapshot is immutable (re-`save` of same id is a no-op); missing id →
  `DENIED`; `count(tenant_id)` and `exists` correct.
- `tests/integration/test_e011_evidence_pipeline.py` — reuses the E-007 harness
  (Qdrant `:memory:`, `ParentChildChunker`, `active_metadata_store`):
  `test_retrieve_evidence_persists_snapshots` confirms each surviving hit yields
  one persisted `Evidence`; `test_deduplication_collapses_same_parent_children`
  confirms multiple children of one parent collapse to a single snapshot.
- `tests/retrieval/test_retrieval_boundary.py` and E-007/E-009 tests MUST stay
  green (no regression from additive `retrieve_evidence`).
- `tests/baseline/` MUST remain green (M0/M1/M2-baseline regression).
- `ruff`, `mypy src/agentic_rag_enterprise`, and the E-011 + relevant regression
  suite all green.

## Acceptance commands
```bash
# E-011 unit + integration
python -m pytest tests/unit/test_deduplication.py tests/unit/test_evidence_store.py tests/integration/test_e011_evidence_pipeline.py -q
# regression: retrieval boundary + E-007/E-009 (additive change must not break)
python -m pytest tests/retrieval tests/integration/test_e007_secure_retrieval.py tests/security/test_e009_authorization.py -q
# baseline must stay green
python -m pytest tests/baseline/ -q
# quality gates
ruff check src/agentic_rag_enterprise tests
mypy src/agentic_rag_enterprise
python -m pytest -q
```
