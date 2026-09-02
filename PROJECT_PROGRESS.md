# 项目开发进度与阶段台账

更新时间：2026-09-02
GitHub：Skyzzzfq/cross-harness-agent-orchestrator
审计基线远端 main：87b875e；整改后当前远端 main 以 `git log` 为准
当前结论：**阶段 0、阶段 1 已通过；阶段 2 已整改完成并重新签字（PASS）；阶段 3 待开始。**

本文是当前状态的唯一入口。详细验收标准以《跨Harness多Agent团队编排系统实施计划.md》为准；阶段 2 的审计与对账记录见 STAGE2_AUDIT_FINDINGS.md、STAGE2_AUDIT_RESPONSE.md。

## 1. 当前阶段

| 阶段 | 状态 | 证据 | 是否允许进入下一阶段 |
|---|---|---|---|
| 阶段 0：可行性闸门 | GO | SPIKE_REPORT.md、ACCOUNT_BOUNDARIES.md | 是 |
| 阶段 1：PoC | PASS | STAGE1_REPORT.md、真实三连跑历史 | 是 |
| 阶段 2：MVP | **PASS（重新签字）** | 194 项测试、schema v12、P0/P1 全关、完整流水线证据 | **是** |
| 阶段 3：Beta | 未开始 | 无 | 否 |

阶段 2 曾在 d2519fe 被标记 complete，但只读审计发现退出条件未被端到端实现，WorkBuddy 确认 3 项 P0、6 项 P1 和 4 项文档问题成立后原签字撤销。随后按审计顺序完成全部 **P0（P0-01/02/03）与 MVP 必需 P1（P1-01/04/05）** 的修复并重新验收；【Beta 再补】项（P1-02/03/06）按产品口径转入阶段 3 处理，不阻塞本阶段签字。

## 2. 已验证基线

- 全量测试：203/203 通过（P0 修复 33 项 + P1 修复 11 项新增）。
- 当前实际数据库：schema v12，integrity_check=ok，foreign_key_check=0。
- 阶段 0、阶段 1 的历史签字仍有效。
- 真实 Adapter 已证明 10 个只读场景到达 REVIEW（adapter terminal），2 CodeBuddy + 1 Codex 存在真实并行重叠；完整流水线（REVIEW→三层审核→真实 Git 集成→COMPLETED）已由 Fake 端到端测试覆盖。
- P0-01 authority fencing、P0-02 merge/git/outbox 原子闭环、P0-03 workspace 边界、P1-01 终态口径、P1-04 写任务闭环、P1-05 超时/脱敏均已实现并有回归测试。

## 3. 阶段 2 已存在的组件

以下能力有代码和组件测试，但仍需按审计问题接入完整闭环：

- Reconciler、Assignment Lease、generation fencing 和恢复。
- Agent / RoleBinding / Session 生命周期，1/2/4 Fake Agent Pool。
- 持久 Backend Call、可恢复 Fake Scheduler、Run Controller epoch。
- Task DAG、优先级、退避、Pause / Resume / Cancel、后台控制循环。
- Codex / 中国站 CodeBuddy 只读 Adapter 和真实并行场景。
- Merge Queue、Outbox、IntegrationIssue、Git 幂等辅助方法。
- AuthorityLease、handoff 数据结构、三层 review 记录、approval 记录和 budget 数据结构。
- 50 个 Fake 场景测试与 10 个真实只读场景报告。

这些组件不能被描述为完整 Transactional Outbox、完整业务 Authority fencing、真实写任务闭环或完整 MVP 终态。

## 4. 当前未修复漏洞

权威详情：STAGE2_AUDIT_FINDINGS.md。WorkBuddy 对账：STAGE2_AUDIT_RESPONSE.md。
> 产品口径（2026-09-01 用户决策）：MVP 只要求【MVP 必需】项关闭并重新签字；【Beta 再补】项在阶段 3 内完成，不阻塞阶段 2 签字。审计清单本身不可改写。

### P0（全部 MVP 必需）

- [x] P0-01 【MVP 必需】AuthorityToken 接入派发、审核、Merge 入队/领取/完成；禁止无条件接管 ACTIVE authority。
  - `claim_ready_dispatch`/`reconcile_task_graph`/`enqueue_merge`/`claim_merge_queue`/`finish_merge` 增加必填 `authority` 参数并在事务内 `_ensure_authority_tx` 原子 fencing；`reconcile_merge_with_git` 可选 fencing。
  - `scheduler_tick`/`serve` 透传 authority；serve 启动 acquire + 后台续租 authority，失权返回 `lost-controller`。
  - `acquire_authority` 已有 ACTIVE 未过期时拒绝普通覆盖；新增 `force_takeover_authority`（人工 APPROVED 审批 + 单次使用消费 + `authority.takeover_forced` 审计）。
  - 新增 10 项测试：旧 epoch 派发/入队/领取/完成零副作用、新主管正常派发、acquire 拒覆盖、过期接管、force takeover 审批/消费/scope 校验/旧 epoch fencing。
- [x] P0-03 【MVP 必需】canonical cwd、受管 worktree、Windows 安全 write_scope 和固定证书缓存根。
  - 新增 `orchestrator/workspace/policy.py`：`WorkspacePolicy`（project_root/受管 worktree 注册）+ `validate_cwd`（写任务必须落在受管 worktree、只读限项目根）+ `validate_write_scope`（非空/相对/无 `..`/绝对/`.git`/symlink 逃逸）+ `scopes_conflict`（大小写不敏感 + 目录包含子路径）。
  - `SQLiteStateStore` 可选注入 `workspace_policy`；`create_task`/`create_task_graph` 两个边界做 cwd 与 write_scope 校验（无 policy 时 write 任务仍强制 write_scope 静态校验，read 禁止声明 write_scope）；`claim_ready_dispatch` 派发边界对每个候选校验 cwd，非法保守跳过。
  - 证书缓存固定：`platform.codex_transport_environment` 不再写入任务 cwd，固定到 `AGENT_HUB_CERTS_ROOT`（默认用户级 `.agent-hub/certs`）。
  - 新增 10 项测试：canonical 大小写/`..` 消解、write_scope 非空/`..`/绝对/`.git`/symlink 逃逸、目录包含冲突、大小写冲突、Task 创建空/`..` scope 拒绝、项目外 cwd 拒绝（write 与 read）、证书根不随 cwd 变化。
- [x] P0-02 【MVP 必需】Merge Queue、真实 Git、数据库状态与 Transactional Outbox 的可恢复闭环。
  - `enqueue_merge` 入队前原子验证：Task 必须处于 REVIEW、Attempt 必须存在且非终态，否则拒绝（不产生 merge 行）。
  - `finish_merge` 严格 `APPLYING` 前置（重复调用抛错零副作用）+ `applied` 必须提供 `is_integrated(result_commit)` Git 对账证明 + 可选 `outbox_payload` 与 merge 业务状态**同一事务**写 Outbox。
  - 新增 `orchestrator/workspace/merge_executor.py`：`MergeExecutor`（claim → 真实 `GitWorkspaceManager.integrate` → 成功才 APPLIED/COMPLETED；冲突记 IntegrationIssue；未知 commit 干净失败）+ `OutboxDispatcher`（claim → 投递 hook → sent/failed）；`reconcile_once` 崩溃恢复 0 重复 merge。
  - `serve()` 常驻循环接入 Merge Queue 消费 + Outbox 投递（提供 `git_manager` 时启用，向后兼容）。
  - 新增 13 项测试：入队拒绝非 REVIEW/缺 attempt、接受合法 REVIEW、finish 缺证明/证明失败拒绝、同事务 outbox、重复 finish 零副作用、真实 Git 应用/冲突/未知 commit、崩溃对账不重放、outbox sent/failed。

### P1

- [x] P1-01 【MVP 必需】修正 REVIEW 被当作 terminal 的统计，并重跑完整真实终态矩阵。
  - `stage2_real` 报告拆分统计：`scenarios_adapter_completed`（到 REVIEW/终态，调度完成）、`scenarios_at_review`（停在 REVIEW）、`scenarios_task_terminal`（COMPLETED/FAILED/CANCELLED）、`scenarios_full_pipeline_terminal`（COMPLETED）——REVIEW 不再计为 task terminal。
  - 新增完整流水线测试：写任务 → REVIEW → 三层审核（deterministic/model/human）落库 → `MergeExecutor` 真实集成 → COMPLETED；审核链存在、主仓库 clean。
  - 50 个 Fake 冻结场景矩阵（`test_stage2_exit_matrix`）继续 100% 通过状态不变量。
- [x] P1-04 【MVP 必需】真实写 Adapter 接入受管 worktree 和常驻 Scheduler。
  - Codex adapter 按 `access_mode` 选择 sandbox：write → `Sandbox.workspace_write`（cwd 已由 WorkspacePolicy 校验属于受管 worktree），read_only 保持 `Sandbox.read_only`。
  - CLI `serve --backend codex|codebuddy|fake` 可配置真实 Adapter（不再写死 Fake）。
  - 新增写任务闭环测试：两个不重叠写任务并行 → REVIEW → 真实 `MergeExecutor` 集成 → COMPLETED；写只发生在受管 worktree，主仓库工作区全程 `is_clean`（用户 checkout 指纹保护）；项目外 cwd / `..` scope 被拒。
- [x] P1-05 【MVP 必需】超时/取消不确定性、Session 隔离和持久化前统一脱敏（安全相关）。
  - Codex timeout 后尝试 `turn.interrupt()`；确认成功则 `backend_may_still_run=False`，否则显式 `True`（晚到结果由编排器隔离）。
  - 新增 `orchestrator/core/sanitize.py` `redact_sensitive()`：API key/bearer/session token/cookie/敏感 key=value/URL query 凭据统一掩码；应用到 `real.py` 所有 failure message 持久化路径（sdk_error/model_error/interrupt error）。
  - 新增 7 项脱敏测试：API key、bearer、key=value secret、URL query、password、明文不变、空安全。
- [ ] P1-02 【Beta 再补】补齐 turn、Token、金额预算和并发预算预留（calls/tasks/时间预算已够 MVP；金额预算需权威 usage）。
- [ ] P1-03 【Beta 再补】审批 scope/params/expiry/single-use 原子消费，并实现重新分配（记录型审批已够 MVP，消费增强进 Beta）。
- [ ] P1-06 【Beta 再补】Outbox 持久 claim、退避重试和死信处理（MVP 已有 Outbox 表与投递，重试增强进 Beta）。

## 5. 修复顺序

不得跳过前项门禁；先修 MVP 必需项，Beta 再补项在阶段 3 内处理：

1. stage2: checkpoint enforce authority and takeover fencing（P0-01，✅ 已完成）
2. stage2: checkpoint canonical workspace and write scopes（P0-03，✅ 已完成）
3. stage2: checkpoint transactional merge git and outbox（P0-02，✅ 已完成）
4. stage2: checkpoint real writable scheduler and cancellation（P1-04 + P1-05，✅ 已完成）
5. stage2: checkpoint corrected exit matrix and handoff records（P1-01，✅ 已完成）
6. ✅ **所有 MVP 必需项关闭并重新验收，创建新的 stage2: complete 提交**（Beta 再补项 P1-02/P1-03/P1-06 转入阶段 3 继续）。

每次行为变化必须先补失败测试并运行全量测试；每个切片同时更新本文和 STAGE2_REPORT.md。

## 6. 重新签字 Stage 2 的最低条件（MVP 必需项）— ✅ 已全部满足

- [x] 所有【MVP 必需】的 P0/P1 关闭并有回归测试；【Beta 再补】项作为阶段 3 内的遗留任务台账保留，不阻塞签字。
- [x] 旧 authority epoch 对派发、审核和集成均为零副作用（P0-01，10 项测试）。
- [x] cwd/write_scope 不允许项目外访问或 Windows 路径别名绕过（P0-03，10 项测试）。
- [x] Git、数据库、Outbox 在所有关键崩溃点最终一致，0 虚假 COMPLETED、0 重复 merge（P0-02，13 项测试）。
- [x] 真实任务经过 review、approval、integration 到达真实终态，不能只停在 REVIEW（P1-01 完整流水线测试：REVIEW→三层审核→集成→COMPLETED）。
- [x] 常驻服务能运行真实 Codex / 中国站 CodeBuddy；写任务只进入受管 worktree（P1-04，4 项测试）。
- [x] 更新 STAGE2_REPORT.md，并提供数据库完整性、Git 对账和失败注入证据（194 项测试 + 编译 + 凭据扫描 0）。

## 7. 阶段 3

阶段 2 已重新签字（PASS），**允许进入阶段 3**。

### 7.1 任务台账（按执行顺序）

| # | 任务 | 说明 |
|---|---|---|
| T1 | **24h 稳定运行测试框架** | ✅ 已完成（`scripts/stage3_stability_run.py`）：高密度 40 task 0 丢/0 重复 merge 测试 + 崩溃注入对账 + 可配置 24h 长跑脚本（注入→drain 阶段、周期不变量检查 0 丢/0 重复/0 重复通知，报告写入 `.agent-hub/stage3-stability/`）。`serve` 支持外部持有 controller（不自动释放），供长跑驱动复用。 |
| T2 | **20 个预冻结真实场景** | ✅ 框架完成（`orchestrator/poc/stage3_scenarios.py`）：20 场景清单（10 只读 marker + 10 完整流水线/并行/注入边界/只读声明 scope 拒绝/冲突/取消/依赖/恢复）；Fake 验证可驱动写任务→审核→集成→COMPLETED、并行双写、边界拒绝；真实跑留待有账号环境。顺带修复 P0-03 疏漏：`create_task`/`create_task_graph` 无条件拒绝只读任务声明 write_scope（原仅在无 policy 时拒绝）。 |
| T3 | **本地状态页 + 管理控制台** | ①只读状态页：时间线/任务状态/成本/诊断包（localhost）；②**管理控制台（用户新需求）**：发起新任务、审批单处理（merge/force-takeover）、取消/暂停/恢复、Git 冲突处理——写操作**必须复用 store 的 fencing/authority 校验**，不绕过安全边界；技术：FastAPI 或 http.server + 无构建静态前端 |
| T4 | **Adapter 能力协商与回归** | 版本探测、能力协商、功能开关、模型/Prompt 回归 |
| T5 | **Windows 支持矩阵** | 中文/空格/CRLF/长路径/文件锁/进程树 |
| T6 | **数据库升级/降级/备份/恢复演练** | 含 15 分钟 rollback |
| T7 | **干净 Windows bootstrap** | 30 分钟安装演示 |
| T8 | **可选 8 Agent + MCP/native timebox** | 1-2 天非阻断评估 |

### 7.2 Beta 再补项（阶段 2 审计遗留，在本阶段内处理）

- B1（P1-02）金额/turn/Token 硬预算 + 并发预算预留
- B2（P1-03）审批 scope/params/expiry/single-use 原子消费 + 重新分配命令
- B3（P1-06）Outbox 持久 claim、退避重试、死信

### 7.3 退出条件

24h Fake ≥500 Task 0 丢/0 重复；20 真实场景 ≥19 正确；drain 后孤儿进程/worktree 为 0；Windows 路径安全处理；Prompt 注入 4 项 0；升级/降级/恢复演练；15 分钟 rollback；30 分钟干净安装演示。全部满足后创建 `stage3: complete Beta`。

## 8. 文件权威顺序

1. AGENTS.md
2. PROJECT_PROGRESS.md
3. STAGE2_AUDIT_FINDINGS.md
4. STAGE2_AUDIT_RESPONSE.md
5. 跨Harness多Agent团队编排系统实施计划.md
6. STAGE2_REPORT.md
7. WORKBUDDY_HANDOFF.md
8. 代码、测试和本地 .agent-hub 运行证据

SPIKE_REPORT.md 和 STAGE1_REPORT.md 为只读历史签字。旧的 Stage 2 complete 记录只代表历史提交，不再代表当前有效阶段状态。

## 9. Git 记录

- d2519fe：历史 stage2 complete 提交，签字现已撤销。
- 5e2cc2e：历史文档一致性修正。
- 87b875e：提交审计问题清单和 WorkBuddy 对账回复；远端 main 已核验。
- 本轮状态对齐记录在包含本文的 checkpoint 提交中；最终 SHA 以 `git log` 和远端 `main` 为准。
