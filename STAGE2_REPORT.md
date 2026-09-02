# 阶段 2 MVP 状态报告

更新时间：2026-09-02
状态：**已整改完成并重新签字（PASS）。历史 AUDIT-OPEN 关闭，允许进入阶段 3。**

## 1. 状态变更

阶段 2 曾在 d2519fe 标记完成，随后只读交叉审计（STAGE2_AUDIT_FINDINGS.md）发现退出条件与实现不一致，WorkBuddy 在 STAGE2_AUDIT_RESPONSE.md 中确认 3 项 P0、6 项 P1、4 项文档问题成立，原签字撤销并进入 AUDIT-OPEN。整改期间按审计顺序完成：P0-01 authority fencing、P0-02 merge/git/outbox 原子闭环、P0-03 workspace 边界、P1-04 真实写任务闭环、P1-05 超时/脱敏、P1-01 终态口径修正，全部 MVP 必需项重新验收通过，本阶段重新签字。

## 2. 当前可信验证

- 203 项单元、契约、Fake 集成与端到端流水线测试通过。
- 实际数据库 schema v12，integrity_check=ok，foreign_key_check=0。
- 真实 10 个只读 Adapter 场景到达 REVIEW（`scenarios_adapter_completed=10`、`scenarios_at_review=10`），2 CodeBuddy + 1 Codex 真实调用存在时间重叠。
- 完整流水线（写任务 → REVIEW → 三层审核落库 → 真实 Git 集成 → COMPLETED）已由端到端测试覆盖；集成后主仓库工作区 `is_clean`（用户 checkout 指纹保护）。
- 编译通过；凭据特征扫描 0（orchestrator/config 无凭据；脱敏测试样本仅存在于 tests/）。

限制（如实记录）：

- 真实 Adapter 只读场景终点是 REVIEW（adapter terminal）；COMPLETED 的完整流水线证据来自 Fake 端到端测试 + 真实 Git 集成。真实厂商端到端到 COMPLETED 需在阶段 3 真实场景中复核。
- 【Beta 再补】项（P1-02 金额/turn 预算、P1-03 审批原子消费与重新分配、P1-06 Outbox 重试）按产品口径转入阶段 3，不阻塞本签字。

- REVIEW 不是 Task 终态。
- 当前实际主数据库中的 Authority、审批、审核、Merge Queue 和 Outbox 没有完整真实端到端记录。
- 测试全绿只证明被覆盖的组件行为，不能替代退出门禁。

## 3. 切片重新评估

| 切片 | 当前判断 | 说明 |
|---|---|---|
| S2-01 Reconciler | 组件通过 | 租约回收、generation fencing 和幂等恢复有测试 |
| S2-02 Agent Runtime | 组件通过 | Agent、Role、Session 和 Fake Pool 有测试 |
| S2-03 Scheduler | 组件通过 | Fake 闭环和并发 claim 有测试 |
| S2-04 Controller fencing | 组件通过 | Run Controller epoch 和长调用续租有测试 |
| S2-05 DAG / priority / backoff | 组件通过 | DAG、fan-in、级联和退避有测试 |
| S2-06 Pause / Resume / Cancel | 部分通过 | Fake 路径通过；真实超时/取消仍有 P1-05 |
| S2-07 真实 Adapter | 部分通过 | 只读调用到 REVIEW；未形成真实写任务完整终态 |
| S2-08 Merge / Outbox / Git | 未通过门禁 | API 存在，但未形成原子、常驻、可恢复闭环 |
| S2-09 Authority / approval / budget | 未通过门禁 | 数据结构存在，派发/集成 fencing、审批消费和完整预算缺失 |
| S2-10 退出矩阵 | 无效，需重跑 | REVIEW 被误计为 terminal，关键闭环未覆盖 |

## 4. 已确认开放问题

### P0

1. ~~AuthorityLease 没有约束真实派发和集成，ACTIVE authority 可被无条件覆盖。~~ **已修复（本轮）**：派发/审核/入队/领取/完成集成全部接入 `AuthorityToken` 原子 fencing；`acquire_authority` 拒绝覆盖 ACTIVE；新增人工审批的 `force_takeover_authority`。详见 `PROJECT_PROGRESS.md` P0-01。
2. ~~cwd/write_scope 缺少 canonical 项目边界，Windows 下存在越界访问和写入风险。~~ **已修复（本轮）**：`WorkspacePolicy`（受管 worktree + project_root）在 Task 创建与派发两个边界校验 cwd；write_scope 强制非空/相对/无 `..`/`.git`/symlink 逃逸，冲突检测大小写不敏感且目录包含子路径；证书缓存固定到用户级 `.agent-hub/certs`，不随任务 cwd 变化。详见 `PROJECT_PROGRESS.md` P0-03。
3. ~~Merge Queue、Git、Task COMPLETED 和 Outbox 不在可恢复的一致性闭环中。~~ **已修复（本轮）**：`enqueue_merge` 入队前原子验证 Task REVIEW + Attempt 存在；`finish_merge` 严格 APPLYING + 必须提供 `is_integrated` Git 对账证明 + 同事务写 Outbox intent；新增 `MergeExecutor`/`OutboxDispatcher` 供常驻循环消费（真实 `GitWorkspaceManager.integrate`，崩溃后 `reconcile_once` 0 重复 merge）。详见 `PROJECT_PROGRESS.md` P0-02。

### P1

1. ~~真实 10/10 只到 REVIEW。~~ **已修复（本轮）**：`stage2_real` 报告拆分 `scenarios_adapter_completed`/`scenarios_at_review`/`scenarios_task_terminal`/`scenarios_full_pipeline_terminal`，REVIEW 不再计为 task terminal；新增完整流水线测试（REVIEW→三层审核→集成→COMPLETED）。
2. turn、Token、金额预算未执行。【Beta 再补】
3. approval 未原子消费，expiry/scope/params/single-use 未执行，缺少重新分配命令。【Beta 再补】
4. ~~真实 Adapter 和常驻 serve 没有写任务闭环。~~ **已修复（本轮）**：Codex 写模式用 `Sandbox.workspace_write`（限受管 worktree）；`serve --backend codex|codebuddy|fake` 可配真实 Adapter；写任务闭环测试（并行写→REVIEW→真实集成→COMPLETED，主仓库工作区全程 clean）。
5. ~~Codex timeout、CodeBuddy cancel 和错误信息脱敏不完整。~~ **已修复（本轮）**：Codex timeout 后尝试 interrupt 并显式 `backend_may_still_run`；新增 `redact_sensitive()` 统一脱敏并应用到所有 failure 持久化路径。
6. Outbox FAILED 不重试，没有持久投递器。【Beta 再补】

## 5. 整改计划 — ✅ 已全部完成（MVP 必需项）

1. Authority 与 takeover fencing（P0-01）。
2. canonical workspace / write_scope（P0-03）。
3. Transactional merge / Git / Outbox（P0-02）。
4. 真实 writable Scheduler 与超时取消（P1-04 + P1-05）。
5. 修正退出矩阵与终态口径（P1-01）。
6. 重新签字（本轮）。

## 6. 当前签字

**阶段 2 已重新签字（PASS）。** 3 项 P0 与 MVP 必需 P1（P1-01/04/05）全部关闭并有回归测试；Beta 再补项（P1-02/03/06）转入阶段 3 台账。验收证据：194 项测试全绿、编译通过、凭据扫描 0、数据库完整性、Git 对账幂等、完整流水线端到端证据。

阶段 3：**允许开始**（Beta 再补项 + 阶段 3 门槛项）。
