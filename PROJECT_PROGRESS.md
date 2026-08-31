# 项目开发进度与阶段台账

更新时间：2026-08-31  
建议 GitHub 仓库名：`cross-harness-agent-orchestrator`  
当前结论：**阶段 0、阶段 1 已完成；阶段 2 进行中；阶段 3 未开始。**

本文是开发交接的状态入口。详细设计以 `跨Harness多Agent团队编排系统实施计划.md` 为准；每个阶段的验收证据分别保存在 `SPIKE_REPORT.md`、`STAGE1_REPORT.md` 和 `STAGE2_REPORT.md`。

## 1. 一眼看懂当前状态

| 阶段 | 状态 | 已有证据 | 是否允许进入下一阶段 |
|---|---|---|---|
| 阶段 0：可行性闸门 | 已完成（GO） | `SPIKE_REPORT.md`、`ACCOUNT_BOUNDARIES.md` | 是 |
| 阶段 1：PoC 行走骨架 | 已完成（PASS） | `STAGE1_REPORT.md`、真实三连跑历史 | 是 |
| 阶段 2：MVP | 进行中 | `STAGE2_REPORT.md`、116 项测试、schema v9 | 否，退出条件未全部满足 |
| 阶段 3：Beta | 未开始 | 无 | 否，必须先完成阶段 2 |

当前实际状态库：schema v9，10 Run、20 Task、29 Attempt、293 Event，`integrity_check=ok`、`foreign_key_check=0`。升级前备份位于 `.agent-hub/state/agent-hub.db.v8-backup-20260831-stage2`；`.agent-hub/` 是本地运行证据，不进入 Git。

## 2. 已完成内容

### 阶段 0：可行性闸门——已完成

- Codex 已使用 ChatGPT Plus 设备授权完成独立登录，不依赖 OpenAI API Key。
- CodeBuddy 使用中国站登录，固定中国服务配置。
- Codex 与 CodeBuddy 均通过受支持的非 GUI 接口完成真实探针。
- 两个 CodeBuddy Session 的上下文和工作目录隔离已验证。
- 成功、失败、超时、取消、Session 恢复均有探针证据。
- 根目录内受控写入成功，越界写入在 Adapter 调用前被拒绝。
- 凭据、Session token 和运行缓存均不进入项目文件或 Git。
- 账号边界、并发、用量和“不自动购买付费能力”约束已记录。

阶段签字证据：`SPIKE_REPORT.md`。

### 阶段 1：PoC 行走骨架——已完成

- 固定团队为 1 个 Codex Supervisor/Reviewer + 2 个 CodeBuddy Worker。
- Team/Role 配置、Task/Attempt 状态机、结构化消息、SQLite 事件审计已落地。
- Fake、Git Fake、Recovery Fake 三条演示链路已完成。
- 两个 Worker 在独立 worktree 中真实并行；同路径冲突被阻断。
- Reviewer 接受 A、驳回 B、B 以新 Attempt 返工后通过。
- Worker 被强制终止后可恢复、重排或明确失败；旧 generation 被 fencing。
- 用户 checkout 的 HEAD、porcelain 状态和内容哈希在演示前后保持不变。
- 固定真实场景连续成功 3 次，且失败样本未删除、未挑样。

阶段签字证据：`STAGE1_REPORT.md`。

### 阶段 2：MVP——已完成的切片

1. 状态 Reconciler
   - Run 隔离查询和显式单次协调。
   - 过期 Assignment Lease 回收、重试耗尽和幂等恢复。
   - generation、续租竞态和事件回滚 fencing。

2. Agent Runtime / Fake Pool
   - AgentInstance、RoleBinding、SessionRef 持久生命周期。
   - 1/2/4 Worker 配置化扩容、4 路重叠和安全 Drain。
   - 统一 Backend Adapter 契约、Fake Adapter 和持久 Backend Call。

3. 可恢复 Scheduler 闭环
   - Task、Agent、Session、Attempt、Lease、Backend Call 原子领取。
   - 并行执行、失败重试、耗尽失败、策略阻塞和崩溃恢复。
   - 两个 SQLite 连接并发领取同一 Task 时只产生一次 Attempt。

4. Run Controller lease / epoch fencing
   - 每个 Run 唯一控制权、续租、过期接管和单调 epoch。
   - claim、Backend 启动授权、运行确认、终态回写和恢复均受当前 epoch 保护。
   - 旧 controller 回调零数据库副作用；接管只恢复旧 epoch 调用。
   - Backend 启动歧义和旧版 NULL epoch 调用会保守隔离 Session。
   - Controller Lease 与 Assignment Lease 均会在调用执行期间自动续租。

5. Task DAG、优先级和退避
   - 原子建图、自环/环/缺失依赖/跨 Run 依赖整批拒绝。
   - fan-in、并发依赖解除、失败/取消传递级联。
   - 到期任务按优先级派发；未来高优先级任务不阻塞当前任务。
   - 指数退避并按上限封顶。

6. Pause / Resume / Cancel 与后台控制循环
   - schema v9：Run 级 `control_state` + Task 级 `paused`，一个调度周期内零新派发，Resume 不绕过 DAG。
   - `request_cancel_task/run`：持久 `CANCEL_REQUESTED`，`starting` 调用零副作用直接取消，`running` 调用 50ms 内发出 interrupt。
   - 取消收敛：任何终态（含自然结束的 SUCCEEDED）收敛到 `CANCELLED`，SUCCEEDED 记为 late，绝不进入审核或集成。
   - 后台常驻循环（`serve`）：controller 常驻续租、失权退出、接管恢复、明确启停；过期租约 90 秒内回收已验证。
   - CLI 新增 `serve / pause / resume / cancel` 命令；环境适配 git 2.55.0 worktree 斜杠分支名回归（`poc/` → `poc-`）。

7. 真实 Codex / CodeBuddy Adapter 接入统一 Scheduler（S2-07，已完成）
   - `orchestrator/adapters/real.py`：Codex（openai_codex SDK，turn.interrupt 取消）与 CodeBuddy（codebuddy_agent_sdk，中国站 internal，SDK 无硬中断则 CANCEL_REQUESTED+late 隔离）统一 BackendAdapter 契约，不复制第二套状态机。
   - `stage2-real` CLI + 冻结 10 个只读场景（5 Codex + 5 CodeBuddy），厂商故障（429/配额/login/sdk_error）与模型内容质量（content_matched）分开统计。
   - **10/10 冻结场景全部通过**：5 Codex + 5 CodeBuddy 全部 REVIEW + content_matched=true，vendor/quality 失败 0。
   - **2 CodeBuddy + 1 Codex 并行验证通过**（`stage2-mixed`）：workers_overlapped=true，真实时间重叠。
   - 真实验证期间 CodeBuddy 曾命中 5 小时用量配额（429，21:35 重置，厂商用量限制），配额恢复后补验即通过，代码路径无需改动。

8. 写范围串行化、Merge Queue、Outbox 与 Git 恢复（S2-08，已完成）
   - schema v10：`merge_queue`（PENDING/APPLYING/APPLIED/CONFLICT/FAILED）、`outbox`（PENDING/SENT/FAILED）、`integration_issues`（write_scope_overlap/content_conflict/unexpected）。
   - `claim_ready_dispatch` 事务内检测同 run ACTIVE 写任务的 `write_scope` 重叠，重叠任务不派发（派发前串行化）；不重叠并行。
   - `enqueue_merge`/`claim_merge_queue` 原子领取（串行）、`finish_merge` APPLIED → task COMPLETED、CONFLICT → IntegrationIssue；`record_outbox_intent`/`claim_outbox`/`finish_outbox` Transactional Outbox。
   - Git 层：`GitWorkspaceManager.integrate` 幂等（`git cherry` 补丁等价检测）、`result_commit_in_integration`、`assert_clean_for_write`（拒脏 checkout）、`safe_write_text`（原子写）、`create_worktree` 失败清理；`reconcile_merge_with_git` 对账已 APPLIED 不重放、遗留 APPLYING 安全重试。

当前验证：135 项单元、契约和 Fake 集成测试通过；无落盘编译通过；凭据特征扫描为 0。详细证据：`STAGE2_REPORT.md`。

## 3. 阶段 2 尚未完成：WorkBuddy 应按此顺序继续

不得跳过前项门禁。每完成一个行为变化都运行全量测试；每完成一个切片都更新 `STAGE2_REPORT.md` 和本文。

### S2-06：完整 Pause / Resume / Cancel 与后台控制循环

- [x] Run/Task 级 Pause：一个调度周期内停止新派发。
- [x] Resume：只恢复允许继续的 READY/PENDING Task，不绕过 DAG。
- [x] Cancel：持久 `CANCEL_REQUESTED`，10 秒内向可中断 Adapter 发出 interrupt（实测 50ms 轮询）。
- [x] 无法强制中止的调用允许自然结束，但结果只能记为 late，不能进入审核或集成。
- [x] 后台 Scheduler/Reconciler 有明确启停、controller 常驻续租、失权退出和接管恢复。
- [x] 证明过期租约在 90 秒内回收。

验收：Fake 覆盖取消前、启动中、运行中、完成竞态、重复取消、失权接管和后台进程重启（15 项新测试）。

### S2-07：真实 Codex / 中国站 CodeBuddy 接入统一 Scheduler

- [x] 把阶段 1 已验证的两个真实 Adapter 接到统一 `BackendAdapter`/Scheduler，不复制第二套状态机（`real.py`）。
- [x] 同时运行 2 个 CodeBuddy Agent + 1 个 Codex Agent（`stage2-mixed`：workers_overlapped=true，真实时间重叠）。
- [x] 冻结 10 个真实场景，至少 9 个获得正确编排终态（10/10 全部正确：5 Codex + 5 CodeBuddy 均 REVIEW + content_matched）。
- [x] 厂商故障与模型内容质量分开统计（`stage2-real` 报告含 `vendor_faults`/`quality_failures`）。
- [x] 继续使用 ChatGPT Plus 登录；不得要求 OpenAI API Key（未引入 API Key，Codex 走 saved-login，CodeBuddy 走中国站账号）。

验收证据：`.agent-hub/reports/run-stage2-real-007520622d15.json`（10 场景）、`.agent-hub/reports/run-stage2-mixed-3ff33b31b53d.json`（并行）。

### S2-08：写范围、Merge Queue、Outbox 与 Git 恢复

- [x] 已声明重叠 `write_scope` 在派发前串行化（`claim_ready_dispatch` 事务内检测同 run ACTIVE 写任务重叠，冲突任务跳过）。
- [x] 未声明重叠或内容冲突进入明确 `IntegrationIssue`（`integration_issues` 表，kind 含 `content_conflict`/`write_scope_overlap`/`unexpected`）。
- [x] 持久串行 Merge Queue；只有审核通过的不可变 result commit 可以入队（schema v10 `merge_queue`，`enqueue_merge`/`claim_merge_queue` 原子领取，`finish_merge` APPLIED → task COMPLETED、CONFLICT → 记 issue）。
- [x] Transactional Outbox 负责数据库提交后的外部副作用（schema v10 `outbox`，`record_outbox_intent`/`claim_outbox`/`finish_outbox`）。
- [x] 进程重启后通过 commit trailer/result commit 对账，证明不重复 merge（`reconcile_merge_with_git` + `GitWorkspaceManager.result_commit_in_integration` 用 `git cherry` 补丁等价检测；已 APPLIED 绝不重放）。
- [x] 脏 checkout、磁盘不足、文件占用和半创建 worktree 均安全拒绝或回滚（`assert_clean_for_write` 拒脏 checkout、`safe_write_text` 原子写不留半写入、`create_worktree` 失败清理 + `worktree prune`）。

当前已通过 135 项测试（新增 7 项 Git 层：integrate 幂等、result commit 补丁检测、脏 checkout 拒绝、原子写安全、worktree 失败清理、merge-with-git 对账 APPLIED/requeue）。

### S2-09：审核、人工审批、预算和业务 Supervisor Handoff

- [ ] 确定性验证、模型审核和人工门禁三层分离。
- [ ] 人类可执行查看、批准、驳回、重新分配和一次性审批命令。
- [ ] 时间、调用数、turn、任务数硬预算；有权威 usage 时才把 Token/金额作为硬门禁。
- [ ] 实现业务 `AuthorityLease`，不要与 Run Controller lease 混淆。
- [ ] Codex → CodeBuddy 两阶段原子主管移交；活动 merge 时拒绝 handoff。
- [ ] 旧业务 epoch 不能派发、审核或集成。

### S2-10：MVP 退出矩阵与签字

- [ ] 50 个预冻结 Fake 编排场景 100% 匹配状态不变量。
- [ ] 10 个真实场景至少 9 个正确编排终态。
- [ ] Worker/Orchestrator 强杀后 0 重复 merge。
- [ ] 完整 Cancel SLA、write_scope、磁盘、脏 checkout、安全负向和费用对账通过。
- [ ] 无未处理高危或严重安全缺陷。
- [ ] 更新 `STAGE2_REPORT.md` 为“通过”，再创建阶段 2 完成提交。

## 4. 阶段 3 尚未开始

只有阶段 2 签字后才能开始：

- [ ] 24 小时 Fake 稳定运行，累计至少 500 Task，0 丢任务、0 重复 merge。
- [ ] 20 个预冻结真实场景至少 19 个正确，0 数据丢失。
- [ ] 本地状态页、时间线、成本和诊断包。
- [ ] Adapter 版本探测、能力协商、功能开关和模型/Prompt 回归。
- [ ] Windows 中文、空格、CRLF、长路径、文件锁和进程树矩阵。
- [ ] 数据库升级、降级、备份、恢复及 15 分钟 rollback 演练。
- [ ] 干净 Windows 环境 30 分钟内完成安装和首次演示。
- [ ] 可选验证 8 Agent；MCP Facade/native team 只做有时限评估。

## 5. WorkBuddy 开发纪律

1. 先读 `AGENTS.md`、本文、实施计划和当前阶段报告。
2. 只执行“阶段 2 尚未完成”中最靠前且未完成的切片。
3. 先写失败测试，再做最小实现；每次行为变化后运行全量测试。
4. 不删除或改写失败证据，不把临时数据库测试冒充真实 Adapter 验收。
5. `.agent-hub/` 只放运行状态、报告、下载工具和缓存；绝不提交凭据或 Session token。
6. Stage N 的全部退出条件未满足时，不得开始 Stage N+1。
7. 阶段完成提交格式：`stageN: complete <short description>`。
8. 阶段尚未完成但需要备份时，只能使用：`stageN: checkpoint <short description>`，不得写 complete。
9. 每次提交前至少执行：

```powershell
python -m unittest discover -s tests -v
git status --short
```

10. 推送后核对远端 commit SHA；只有阶段报告、本文和远端提交三者一致才算完成。

## 6. Git 历史说明

本项目在阶段 2 进行中才初始化正式远端仓库，因此早期源代码没有原始逐步 commit 可恢复。首次发布会诚实采用以下结构：

1. 导入当前代码基线。
2. 单独提交阶段 0 的签字证据。
3. 单独提交阶段 1 的签字证据。
4. 提交阶段 2 的“进行中检查点”，不标记为完成。

以后严格在每个阶段全部退出条件通过后创建一次 `stageN: complete ...` 提交并推送。
