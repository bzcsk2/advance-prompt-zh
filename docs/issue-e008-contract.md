# E-008 Issue Contract — Idempotent Ingestion Job & Active-Version Protocol

- **Milestone:** M1 — Secure single-corpus data vertical slice
- **Depends on:** E-005 (domain models, lifecycle state machine, migrations `001`), E-006 (SecurityContext, ACL truth table, `build_access_filter`/`evaluate_access`/`resource_passes_filter`), E-007 / E-007.1 (ported `ParentChildChunker`, Qdrant `VectorStore` + `ParentStore`, `SecureRetriever` + `ParentReader`, `child_chunk_to_point`).
- **Migration required:** yes (`002_add_lifecycle_revision.sql`).
- **Rollback:** revert commit; down-migrate `documents.lifecycle_revision` (column drop) or recreate metadata DB from `001`; data-plane (Qdrant / Parent Store) is rebuildable from Metadata DB, so a fresh `metadata.db` + re-ingest restores state.

```yaml
id: E-008
milestone: M1
depends_on: [E-005, E-006, E-007, E-007.1]
allowed_paths:
  - src/agentic_rag_enterprise/storage/metadata_store.py        # NEW
  - src/agentic_rag_enterprise/storage/parent_store.py          # extend (deprecate)
  - src/agentic_rag_enterprise/ingestion/job.py                 # NEW (DocumentManager / IngestionJob)
  - src/agentic_rag_enterprise/ingestion/chunker.py             # reuse (no change required)
  - src/agentic_rag_enterprise/domain/ingestion.py              # reuse (no change required)
  - src/agentic_rag_enterprise/domain/document.py               # reuse (no change required)
  - src/agentic_rag_enterprise/domain/chunk.py                  # reuse (no change required)
  - src/agentic_rag_enterprise/security/                        # reuse policy.py / filter.py (no change)
  - src/agentic_rag_enterprise/retrieval/                       # reuse retriever/hybrid/parent_reader (verify only)
  - src/agentic_rag_enterprise/storage/vector_store.py          # reuse child_chunk_to_point (no change)
  - config.py                                                    # add metadata_db_path (injected)
  - migrations/002_add_lifecycle_revision.sql                   # NEW
  - tests/{unit,integration,security,fixtures}/                 # NEW tests
  - AGENTS.md
  - docs/issue-e008-contract.md
forbidden_paths:
  - /vol4/Agent/agentic-rag-for-dummies                         # upstream read-only
  - src/agentic_rag_enterprise/agents/                          # not in scope
  - src/agentic_rag_enterprise/graph/                           # not in scope
  - src/agentic_rag_enterprise/api/                             # not in scope (wiring deferred)
  - src/agentic_rag_enterprise/retrieval/security truth tables  # reuse, do not re-authorize
migration_required: true
rollback: "revert commit; drop documents.lifecycle_revision; rebuild metadata.db from 001 + re-ingest (data-plane is rebuildable)"
acceptance_tests:
  - tests/unit/test_metadata_store.py
  - tests/unit/test_ingestion_job.py
  - tests/integration/test_e008_ingestion_e2e.py
  - tests/integration/test_e008_crash_points.py
  - tests/baseline/                                  # must stay green
required_docs:
  - docs/issue-e008-contract.md
```

## 1. 目标 (Goals)

实现 build plan §10 规定的文档摄取控制面：

- 幂等的摄取 Job（同一 `document_id + version` 重复摄取 → skip，不重复写 Chunk / 向量）。
- active-version 协议：新版本先写数据面，验证完整后，在 **Metadata DB 单一事务** 中切换 active version；旧版本标记 inactive / superseded。
- Metadata DB 作为摄取控制面 **唯一事实源**；Qdrant、Parent Store、文件系统为可重建数据面。
- 跨存储一致性协议（build plan §10.10）逐条落地，包含 4 个 crash-point 集成测试。

## 2. 非目标 (Non-goals)

- PDF 解析（`document_chunker.py` 仅 port 了 Markdown heading-aware chunking；PDF→MD 在 capability map 中标为 `scaffold`/gap）。本 Issue 摄取入口接受 **已转换的 Markdown 文本** 作为内容输入；PDF 解析留给后续 Issue。
- 逻辑删除 / 物理 purge（build plan §10.6）与 ACL 收紧（§10.7）属于 **E-010**，不在本 Issue 范围。但 lifecycle 状态机与 `lifecycle_revision` 机制为它们预留。
- Planner、Evidence Store、多 Corpus 路由、Reranker 等后续 Milestone 能力（build plan §12 等）——不提前实现。
- 独立 Reconciler 守护进程（build plan §10.10 #7 的“reconciler 重试”）——本 Issue 通过 **可重入的 Job 重跑** 实现等价补偿/恢复；常驻 reconciler 留给 M5（E-022）。

## 3. 与 M1 的关系

M1 范围为 “稳定 Document/Version/Chunk Schema、Metadata DB migration、SecurityContext、ACL 真值表、幂等 Job、active version 切换、Parent/Child 复用、Qdrant hybrid retrieval、Parent 二次授权”（build plan §22）。

本 Issue 完成：**幂等 Job + active version 切换 + Metadata DB 作为控制面**。E-007/E-007.1 已完成 retrieval 与 Parent 二次授权。`ingest → retrieve` 闭环在本 Issue 打通；`update` 通过 active-version 切换（内容变化→新版本→切换）自然覆盖；`delete`/`ACL 收紧` 由 E-010 收口，M1 exit gate 由 E-008 + E-010 共同满足。

## 4. 复用方案 (Reuse)

| 能力 | 来源 | 复用方式 |
|---|---|---|
| Parent-child chunking | `ingestion/chunker.py:ParentChildChunker` (E-007 port) | 直接复用 `chunk_markdown(text, tenant_id, corpus_id, document_id, document_version)` |
| Qdrant hybrid 写入 | `storage/vector_store.py:VectorStore` + `child_chunk_to_point` (E-007.1) | 复用 `upsert` 与 `child_chunk_to_point(child, acl, status=..., deprecated=...)`；`status` 由 Job 控制（写入=`processing`，发布=`active`） |
| Parent Store | `storage/parent_store.py:ParentStore` (E-007) | 复用 `put`/`get`；新增 `deprecate(parent_id)` 用于切换时标记旧版本 |
| 生命周期状态机 | `domain/ingestion.py:DocumentStatus/JobStatus/valid_transition` | 直接复用 |
| Manifest | `domain/ingestion.py:IngestionManifest` (§10.9) | 直接复用记录 Job 结果 |
| ACL 真值表 / PEP | `security/policy.py:ResourceAcl` + `security/filter.py:build_access_filter` | 复用；不改动授权语义 |
| 检索与 Parent 二次授权 | `retrieval/retriever.py` + `parent_reader.py` | 复用验证：检索只可见 `status=active & deprecated=false` 的 Chunk（build plan §10.10 #5） |
| 上游 `DocumentManager.add_documents` 行为 | `agentic-rag-for-dummies/project/core/document_manager.py` | 行为基线（parse→chunk→parent store→qdrant→失败补偿删除已写 parent）。**目标实现不 import 上游**；新建 `DocumentManager` 复用该链路语义并加上 Manifest / 幂等 / 版本 / 状态。 |

## 5. 数据模型与 Migration 变化

### 5.1 复用既有模型（不重新定义）

- `domain/document.py:SourceDocument` —— 对应 build plan §7.3。
- `domain/chunk.py:ChunkRecord` —— 对应 build plan §7.4。
- `domain/ingestion.py:DocumentStatus`（discovered/processing/active/failed/deprecated/deleted）、`JobStatus`、`IngestionManifest`（§10.9）。

### 5.2 Migration `002_add_lifecycle_revision.sql`

`001_initial_schema.sql` 已满足：

- `documents` 主键 `(document_id, tenant_id, corpus_id, version)` —— §10.10 #1（唯一约束覆盖四元组）。
- 部分唯一索引 `idx_documents_active_version ON documents(tenant_id, corpus_id, document_id) WHERE status='active'` —— 实现 “同一文档至多一个 active 版本”，是 active-version 切换的 DB 层锁（§10.10 #2、#4）。
- `ingestion_jobs` 主键 `job_id` —— §10.10 #1。

新增（E-008）：`documents.lifecycle_revision INTEGER NOT NULL DEFAULT 0`，满足 §10.10 #8（单调 revision 决定删除/更新竞争顺序；旧 revision Job 不得覆盖新状态）。提交 active 切换时在 **同一事务** 内 `lifecycle_revision = (SELECT MAX(...)+1)`，并以 `WHERE lifecycle_revision = :expected` 做 CAS。

## 6. 幂等语义 (build plan §10.4)

- 同一 `document_id + version` 重复摄取：
  - `documents` 已有该 `(tenant,corpus,doc,version)` 且 `status='active'` → `ingest()` 返回 `IngestionResult(status=ALREADY_INDEXED)`，不生成重复 Chunk、不重复写向量。
  - 同一 `job_id` 重复投递 → `ingestion_jobs` 主键约束 + step marker → 重跑为 **可重入幂等**，不产生新业务 ID、不重复 Chunk（§10.10 #3）。
- 同一 `document_id` 内容变化（hash 不同 → 新 `version`）：
  - 新版本进入 `processing` → 临时写入数据面 → 验证完整 → 原子切换 active version → 旧版本标记 `superseded`/`inactive`（§10.4）。

## 7. active-version 协议 (build plan §10.2 / §10.10)

摄取 Job 步骤（`metadata_store.job_steps` 记录每步完成状态，可重入）：

1. `acquire` —— CAS 领取 Job（INSERT `ingestion_jobs`）；已 succeeded → 幂等跳过；已 running → 恢复。
2. `parse` —— 计算 `raw_hash`；若 `(tenant,corpus,doc,version)` 已存在则按状态决策（active→skip / processing→resume / failed→reprocess）。
3. `chunk` —— `ParentChildChunker.chunk_markdown(...)`。
4. `write_parents` —— `ParentStore.put` 每个 parent（补充 ACL 元数据，同 E-007.1 e2e 约定）。
5. `write_qdrant` —— `child_chunk_to_point(child, acl, status="processing", deprecated=False, ...)` 后 `VectorStore.upsert`。**写入时状态为 `processing`，因此未被提交的版本对检索不可见**（§10.10 #5）。
6. `commit` —— Metadata DB 事务内：
   - `lifecycle_revision = (current max) + 1`，CAS `WHERE lifecycle_revision = :expected`（§10.10 #8）；
   - 旧 active 行 `status='superseded'`、`effective_to=now`；
   - 新行 `status='active'`、`effective_from=now`、`indexed_at=now`；
   - 利用 `idx_documents_active_version` 部分唯一索引保证至多一个 active（§10.10 #2、#4）。竞争失败抛 `ActiveVersionConflict`（fail-closed，Job 标记 failed，不破坏旧 active）。
7. `publish` —— **幂等、可重试**（§10.10 #7）：
   - 新版本 Qdrant 点重 upsert `status='active'`（point id 为稳定 `uuid5`，同 id 覆盖，幂等）；
   - 旧版本 Qdrant 点与 Parent Store 标记 `inactive`/`deprecated=True`（检索 `status=active & deprecated=false` 过滤排除）。
8. `finalize` —— `ingestion_jobs.status='succeeded'`、`finished_at=now`；写 `IngestionManifest`。

顺序严格遵循 build plan：processing → 数据面写入 → 验证 → active-version 提交 → 发布（§10.4 “新版本必须按 processing、数据面写入、验证、active-version 提交顺序执行”）。

## 8. 失败补偿与恢复 (build plan §10.5 / §10.10 #3,#6,#7)

- 提交（step 6）**之前** 失败：补偿删除本版本产生的 Parent、Child（Qdrant 点 + Parent Store 条目）；**不删除已生效旧版本**（§10.5）。补偿本身幂等。
- 提交（step 6）**之后** 清理（step 7）失败：不回滚已可见的新版本；由 **Job 重跑**（reentrant，跳过已完成 step）重试 publish，直到数据面与 Metadata DB 一致（§10.10 #6、#7）。
- Step marker 可重入（§10.10 #3）：重跑相同 Job 不生成新业务 ID、不重复 Chunk。
- Job 领取用 DB 唯一约束 / 行级事务（§10.10 #2）。

## 9. crash-point 测试 (build plan §10.10 末段，强制)

`tests/integration/test_e008_crash_points.py` 至少覆盖：

1. **Parent 写入后崩溃** —— `run(max_step="write_parents")` 后停止；验证未提交版本对检索不可见；重跑 `run()` 完成并一致。
2. **Qdrant 写入后崩溃** —— `run(max_step="write_qdrant")` 后停止；验证检索仍只返回旧 active 版本、新版本（status=`processing`）不可见；重跑完成。
3. **active version 切换后清理失败** —— 模拟 step 7 抛错；验证 DB 已切换 active、旧版本不可见；重跑 publish 成功、数据面一致（不回滚新版本）。
4. **重复投递同一 Job** —— 两次 `run()`；验证仅一个 active 版本、无重复 Chunk、相同 `job_id`。

## 10. 测试与完成标准 (build plan §23)

- 单元：`tests/unit/test_metadata_store.py`（schema/migration 应用、唯一约束、active-version 切换事务、lifecycle_revision CAS）、`tests/unit/test_ingestion_job.py`（幂等、步骤重入、补偿）。
- 集成：`tests/integration/test_e008_ingestion_e2e.py`（ingest→retrieve 闭环、内容更新→新版本切换、tenant/corpus 身份链）、`tests/integration/test_e008_crash_points.py`（§9 四点）。
- 安全：复用 `tests/security/test_parent_reader.py` 与 `tests/integration/test_qdrant_authorization.py` 保持绿色；新增 tenant/corpus 绑定验证（ingest 时 `acl.tenant_id == document.tenant_id` 由 `child_chunk_to_point` 守卫，E-007.1 已落）。
- 全部测试本地、确定性、hermetic：临时 `metadata.db`、内存/本地 Qdrant、Fake encoder；不依赖真实 LLM / 外网 / 模型下载。
- 完成标准：`ruff` / `mypy src/agentic_rag_enterprise` / `pytest` / `pytest tests/baseline` 全绿；baseline 全通过。

## 11. 兼容性 / 回滚

- 向后兼容：`002` 仅为 `documents` 增加 `lifecycle_revision` 列（默认 0），不破坏既有查询。
- 既有 retrieval 路径（`build_access_filter` 要求 `status=active & deprecated=false`）**不改**；active-version 隔离由数据面 status 标记实现，与既有过滤正交。
- 回滚：revert 本 commit；`ALTER TABLE documents DROP COLUMN lifecycle_revision` 或重建 `metadata.db`（数据面可重建）。E-007/E-007.1 行为不受影响。

## 12. 已知限制（明确不纳入 E-008）

- PDF 解析未实现（接受 Markdown 文本输入）。
- 逻辑删除 / 物理 purge（§10.6）与 ACL 收紧（§10.7）留给 E-010。
- 常驻 Reconciler 守护进程留给 M5（E-022）；本 Issue 以可重入 Job 重跑实现等价恢复。
- Embedding / Chunking 升级切换（§10.8）留给后续，本 Issue 仅记录 `parser_version`/`chunking_version`/`embedding_version` 字段（模型已存在）。
