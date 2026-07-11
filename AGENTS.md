# AGENTS.md — Agentic RAG Enterprise

## Implementation Spec
`docs/agentic-rag-enterprise-build-plan.md`

## Current Milestone & Issue
- Milestone: **M1** — Secure single-corpus data vertical slice
- Issue: **E-006** — Add SecurityContext, policy truth table and authorization tests

## Fixed Paths
```bash
UPSTREAM_REPO=/vol4/Agent/agentic-rag-for-dummies
TARGET_REPO=/vol4/Agent/agentic-rag-enterprise
```

## Fixed Commits (M1 baseline)
- Target: `3748b33ffa37a0f977d9ba448e6d760a639b5eba` (main)
- Upstream: `8b3e5ff0619f7ede593d728e4a8b459fbbec9b08` (main, tag v2.3)

## Permanent Rules (all milestones)
1. **DO NOT modify upstream** (`/vol4/Agent/agentic-rag-for-dummies/`).
2. Target uses `src/agentic_rag_enterprise/` package layout.
3. `pyproject.toml` is the single source of truth for dependencies.
4. Do not create empty code directories.
5. Keep existing working tree changes; do not reset, checkout, or overwrite.

## E-005 Allowed Changes (M1 only) — completed
- `src/agentic_rag_enterprise/domain/` — create or modify domain models
- `migrations/` — create or modify migration scaffolding
- `tests/test_domain_models.py` — create or modify
- `AGENTS.md` — update
- Do not modify existing modules under `src/agentic_rag_enterprise/{agents,graph,retrieval,api,evals,observability,ingestion,security,config,schemas,providers}`.
- No upstream modifications. No push, no PR creation.

## E-006 Allowed Changes (M1 only)
- `src/agentic_rag_enterprise/security/` — create or modify policy truth table, PEP filter, authorization
- `src/agentic_rag_enterprise/domain/security.py` — may be read; SecurityContext already matches spec §7.5
- `tests/security/` — create authorization tests (truth table, corpus discoverability, PEP filter)
- `AGENTS.md` — update
- Keep `security/policy.py:AccessPolicy.can_access(user_id, corpus)` shim so the M0 baseline
  characterization tests in `tests/baseline/test_retrieval_baseline.py` stay green.
- No upstream modifications. No push, no PR creation.

## Standard Checks
```bash
# Before starting a task
cd $TARGET_REPO
git status --short
git branch --show-current
git rev-parse HEAD

cd $UPSTREAM_REPO
git status --short
git rev-parse HEAD

# After completing a task
cd $TARGET_REPO
git diff --check
git status --short
```
