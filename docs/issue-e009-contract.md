# E-009 Issue Contract (M1) — parent-store secondary authorization

Audit-remediation / capability-completion of the E-007 ported `ParentReader` second-auth
path, brought up to the build plan's normative requirements:

- **§12.5 Parent 二次权限校验** — even when Child Search already filtered, reading a
  Parent must re-validate: (1) Parent metadata consistent with Child metadata;
  (2) Parent visible to the current user; (3) Parent not deleted; (4) Parent version
  still the hit's version; and **禁止仅凭模型返回的 `parent_id` 直接读取文件**.
- **§12.9 失败语义 (parent-store)** — parent-store secondary authorization failures
  carry distinct codes: `PARENT_NOT_FOUND`, `PARENT_NOT_AUTHORIZED`,
  `DOCUMENT_DELETED`, `VERSION_MISMATCH`; and "对用户输出时避免泄露无权限资源是否存在".

Baseline `d03d92b` (E-008.4 CLOSED) is kept intact; E-009 is a narrow, plan-mandated
capability-completion commit. Scope is a strict subset of the M1 retrieval/storage
paths (`retrieval/`, `storage/`, `security/`, `tests/...`, `docs/`,
`AGENTS.md`); no upstream modifications, no `config.py`/`domain/` changes.

### Closure patch (audit verdict: CONDITIONAL FAIL)
Review of `74c298d` found `RetrievalResult.denied_reasons` was a plain public
Pydantic field, so `model_dump()`/`model_dump_json()` serialized the §12.9 codes to
the end user — violating the E-009 contract ("non-user-exposed") and build plan §12.9
("avoid leaking existence"). Fix: `denied_reasons` is now `Field(default_factory=dict,
exclude=True)` — excluded from all serialization, still readable internally for
telemetry. The integration test asserts `model_dump()` omits `denied_reasons` and the
JSON form contains none of the §12.9 codes. `74c298d` is NOT rolled back.

## Goal
Make the parent store readable **only** through the authorized second-auth path, and
classify every parent-auth denial with the §12.9 code — without ever leaking to the
end user whether an unauthorized resource exists.

## depends_on
- **E-007** — ported `ParentReader` + second-auth (baseline `ccb52dc`); the
  `load_parent_for_hit` entry point already exists and is the single authorized accessor.
- **E-007.1 P1-2** — `ParentReader` fail-closed on missing/malformed auth metadata.
- **E-008.3 P1-7** — `SecureRetriever.metadata_store` is mandatory; the parent pass runs
  only after the control-plane active-version gate (retriever.py:135-139), so the parent's
  version is guaranteed the active version before `ParentReader` re-checks it.
- **E-006** — `SecurityContext` + `build_access_filter` / `resource_passes_filter` PDP/PEP
  truth table (the second-auth PDP projection).

## Non-goals (M1 only)
- No second retrieval runtime; extend the E-007 ported chain.
- No logical delete / ACL-tightening (that is E-010).
- No new encoders/config in `config.py` / `Settings`; inject as E-007 did.
- No weakening of the fail-closed `ParentReader`; no filter-less retrieval.

## Design decisions (RECOMMENDED defaults — confirm before implementation)
1. **Error taxonomy** — add typed subclasses of the existing
   `ParentAuthorizationError` (retrieval/models.py:76) so the current
   `except ParentAuthorizationError` in retriever.py:142 keeps working:
   - `ParentNotFoundError` → `PARENT_NOT_FOUND` (id absent from store / untrusted guessed id;
     parent_reader.py:100-104).
   - `ParentNotAuthorizedError` → `PARENT_NOT_AUTHORIZED` (tenant/corpus mismatch
     parent_reader.py:110-114, missing/malformed auth metadata :67-89, parent/child ACL
     mismatch :154, `resource_passes_filter` denied :158).
   - `ParentDeletedError` → `DOCUMENT_DELETED` (status != "active" or deprecated;
     parent_reader.py:128-132).
   - `ParentVersionMismatchError` → `VERSION_MISMATCH` (parent.document_version !=
     hit.document_version; parent_reader.py:117-118).
2. **No un-authorized direct read** — `ParentStore.get` (parent_store.py:26) stays as an
   *internal* accessor (still used by ingestion/job.py for verify/publish, which is out of
   E-009's allowed scope and must NOT be edited). Closure is enforced by an **architecture
   test** (`tests/unit/test_retrieval_boundary.py`) asserting the retrieval/API surface
   (`retriever.py`, `api/`) never imports/calls `ParentStore.get`; the only authorized
   retrieval accessor remains `ParentReader`. (Renaming `get`→`_get` would require editing
   `ingestion/job.py`, outside the allowed paths, so it is NOT done here.)
3. **Denial accounting** — keep `RetrievalResult.denied_parent_count: int` (E-007.1
   contract); ADD a supplementary, non-user-exposed `denied_reasons: dict[str, int]`
   (keys = §12.9 codes) for telemetry. The user-facing `RetrievalResult` still omits any
   per-parent detail, preserving §12.9 "avoid leaking existence".

## Allowed paths (M1 only)
- `src/agentic_rag_enterprise/retrieval/parent_reader.py` — map each §12.5 check to a
  §12.9 typed error; keep fail-closed.
- `src/agentic_rag_enterprise/retrieval/models.py` — add the error subclasses (under
  `ParentAuthorizationError`).
- `src/agentic_rag_enterprise/retrieval/retriever.py` — record denied reason without
  leaking; keep `denied_parent_count`, add internal `denied_reasons`.
- `src/agentic_rag_enterprise/storage/parent_store.py` — docstring only (mark `get` internal);
  no behavioral change needed (closure via architecture test).
- `tests/security/test_parent_reader.py` — extend with §12.9 per-code parametrization.
- `tests/unit/test_retrieval_boundary.py` — NEW; forbids `ParentStore.get` on the
  retrieval/API path.
- `tests/integration/test_e009_parent_secondary_auth.py` — NEW; full-pipeline each-denied-reason
  maps to the correct §12.9 code and the user-facing result never reveals existence.
- `docs/issue-e009-contract.md` (this file), `AGENTS.md`.

## Forbidden
- No upstream modifications; no target import of upstream paths.
- No `config.py` / `domain/` changes.
- No editing of `ingestion/job.py` (out of allowed scope; `ParentStore.get` stays usable there).
- MUST NOT make the retrieval/API path reach `ParentStore.get` (architecture test enforces).
- MUST NOT weaken fail-closed `ParentReader`; MUST NOT expose denied-reason detail to the
  end user.

## Acceptance tests
- `tests/security/test_parent_reader.py` — parametrized: each §12.5 check raises the correct
  §12.9 subclass (`ParentNotFoundError` / `ParentNotAuthorizedError` / `ParentDeletedError` /
  `ParentVersionMismatchError`); missing/malformed auth metadata → `ParentNotAuthorizedError`
  (preserves E-007.1 P1-2).
- `tests/unit/test_retrieval_boundary.py` — `retriever.py` / `api/` do not import/call
  `ParentStore.get`; `ParentReader` is the only authorized accessor.
- `tests/integration/test_e009_parent_secondary_auth.py` — ingest → retrieve child → parent
  load goes through `ParentReader`; each denied reason maps to the right §12.9 code; the
  `RetrievalResult` exposes no per-parent existence detail; `denied_parent_count` still sums.
- `tests/baseline/` MUST remain green (M0/M1 regression).
- `ruff`, `mypy src/agentic_rag_enterprise`, full `pytest` (incl. `tests/baseline/`) all green.

## Acceptance commands
```bash
python -m pytest tests/security/test_parent_reader.py tests/unit/test_retrieval_boundary.py tests/integration/test_e009_parent_secondary_auth.py tests/baseline/ -q
ruff check src/agentic_rag_enterprise tests
mypy src/agentic_rag_enterprise
python -m pytest -q
```
