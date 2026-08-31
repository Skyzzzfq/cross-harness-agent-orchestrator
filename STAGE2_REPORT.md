# 阶段 2 MVP 进度报告

更新时间：2026-08-31

状态：**进行中。前六切片“状态 Reconciler”“Agent Runtime / Fake Pool”“可恢复 Fake Scheduler 闭环”“Run Controller fencing”“Task DAG / 优先级退避”和“Pause / Resume / Cancel 与后台控制循环”已完成，尚未满足阶段 2 全部退出条件。**

## 本切片范围

- `summary(run_id=...)` 和 `status --run <run-id>`：隔离查询单个 Run 的 Task、Attempt、Message 和 Event 统计。
- `reconcile --run <run-id>`：显式执行一次协调，只扫描已过期、Lease 仍为 `ACTIVE`、Attempt 为 `ASSIGNED/RUNNING` 且 Task 为 `ACTIVE` 的记录。
- 恢复复用 generation fencing 和原有事务状态迁移：有重试额度时 Task 回到 `READY`，耗尽时进入 `FAILED`；Attempt 进入 `STALE`，Lease 进入 `EXPIRED`。
- 扫描与回收之间使用同一 cutoff，并通过带 generation、状态和 `expires_at` 条件的更新重新认领；扫描后已续租的 Worker 不会被误回收。
- 硬过期 Lease 不能再 heartbeat 或提交结果。
- Recovery Fake 已改为由 Reconciler tick 回收被杀死的 Worker，而不是测试代码直接调用底层恢复函数。

## 验证结果

- 49 项单元、契约和 Fake 集成测试全部通过。
- 已覆盖：Run 隔离、未知 Run、未过期不处理、过期回收、重试耗尽、重复 tick 幂等、扫描后续租竞态、过期心跳/提交 fencing、事件失败整笔回滚和 Worker kill/recovery。
- 真实历史 Run `run-real-c4f1c4bb6545` 处理前：2 个 `ACTIVE` Task、2 个 `RUNNING` Attempt、2 个已过期 ACTIVE Lease。
- 第一次协调：Worker A → `STALE/FAILED`，Worker B → `STALE/READY`，新增 4 条状态事件。
- 第二次协调：检查 0 条、恢复 0 条、事件数保持不变。
- 首次协调报告：`.agent-hub/reports/reconcile-5d9f90c4e0de.json`；幂等复核：`.agent-hub/reports/reconcile-04a5bee9cf3c.json`。

## 第二切片：Agent Runtime / Fake Pool

- 增加 `AgentInstance`、`RoleBinding`、`SessionRef` 生命周期和持久化模型；同一 Agent 在同一 Run 只能有一个 active primary RoleBinding，不同 Agent 或不同 Run 不互相误伤。
- schema v3 将历史 Attempt 中可判定的 5 个 Agent 迁移成 `MIGRATED` 实例，不猜测或伪造历史 RoleBinding / SessionRef；迁移前保留 `.agent-hub/state/agent-hub.db.v2-backup-20260831`。
- 冻结统一异步 `BackendAdapter.start()`、`RunningCall.wait()/cancel()` 契约和 Fake Adapter：调用幂等、单 Session 单 active call、不同 Session 并行、轮询超时无副作用、执行期限终态、确认/未确认取消、调用者取消隔离和不可变结果。
- 策略阻塞会立即返回 `BLOCKED/backend_invoked=false`，不会伪造一次 Backend 启动；取消/完成竞态只保留一个稳定终态。
- schema v4 增加持久 `backend_calls`：Adapter 启动前先写 `starting` 意图，只保存请求摘要，不保存 Prompt 明文；记录 Session、Attempt、generation、厂商调用引用、终态、Usage 和 late-result 标记。
- 模糊 `starting` 在重启恢复时进入 `ORPHANED`，对应 Attempt 被 generation fencing；旧 generation 的晚到成功只留档，不能提交。当前 generation 的成功终态与 Attempt `SUBMITTED`、Task `REVIEW`、Lease 释放在同一事务提交。
- Fake Agent Pool 已验证 1→2→4 只补缺口、重复 tick 幂等、4 路真实时间重叠、Run 隔离和缩容到 0。忙碌实例缩容只进入 `DRAINING`，不强杀；任务释放后的下一 tick 才关闭 Session、结束 RoleBinding 并停止 Agent。
- 实际运行库已升级至 schema v4；旧 Task / Attempt / Message / Event 计数保持不变，`foreign_key_check=0`，`integrity_check=ok`。
- 74 项单元、契约和 Fake 集成测试在第二切片结束时全部通过。

## 第三切片：并发调和与可恢复 Scheduler 闭环

- schema v5 增加按 `(run_id, pool_id)` 唯一的短期调和锁。两个独立 SQLite 连接同时协调同一 Pool 时只有一个写入者，另一方返回 `busy`；锁过期后可自动接管，避免重复扩容和重复事件。
- 增加每个 Agent / Run 最多一个可用 Session 的部分唯一索引，避免异常 Session 数据放大 Pool 容量。
- schema v6 增加持久 `task_dispatch_specs`，保存 required role、指令、cwd、timeout、priority 和 available time；历史 Task 不猜测补值，因此不会被新 Scheduler 意外派发。
- `claim_ready_dispatch()` 使用 `BEGIN IMMEDIATE` 原子完成 Task `READY→ACTIVE`、Agent `IDLE→BUSY`、Session `IDLE→ACTIVE`、Attempt / Lease / BackendCall 创建和审计事件；Role 不匹配、Agent 非 IDLE 或 Session 非 IDLE 时不会领取。
- `scheduler_tick()` 会先领取当前可用容量，再并行执行各 Session；同一 tick 内的失败重排不会自旋重试，下一 tick 才创建新 Attempt / generation。
- Backend 成功会原子推进 Attempt `SUBMITTED`、Task `REVIEW`、释放 Lease / Agent / Session；重复终态回调直接返回原 disposition，不修改状态或重复追加事件。
- retryable failure / timeout 在额度内回到 `READY`，耗尽后进入 `FAILED`；`BLOCKED` 在零 Backend 启动下进入明确失败终态，且不会泄漏容量。
- 启动前崩溃会将 Call 标记 `ORPHANED`、Attempt 标记 `STALE` 并安全释放 Agent / Session；运行中崩溃会隔离 Session、将 Agent drain，随后由 Pool 停止旧实例并补充替代实例。
- 两个独立 Scheduler 连接同时领取同一 Task 时，只产生一个 Attempt、Lease 和 BackendCall；DRAINING Agent 永不被派发。
- 实际运行库已升级至 schema v6；历史 Task / Attempt / Message / Event 计数保持不变，历史 Task 的 dispatch spec 数为 0，`foreign_key_check=0`，`integrity_check=ok`。
- 第三切片结束时共 83 项单元、契约和 Fake 集成测试全部通过。

## 第四切片：Run Controller lease / epoch fencing

- schema v7 增加每个 Run 唯一且永不删除的 `run_controller_leases`；首次获取 epoch 为 1，租约过期接管或显式 handoff 只递增一次，同一 owner 续租保持 epoch 不变。
- `ControllerToken(run_id, owner_id, epoch, expires_at)` 贯穿 Scheduler 的领取、Backend 启动授权、启动确认、终态回写和恢复；所有受保护状态变更都在 `BEGIN IMMEDIATE` 事务内重新核对当前 owner、epoch 和过期时间。
- `backend_calls` 持久记录 `scheduler_owner/controller_epoch`。旧 controller 的 claim、mark、finish 和 recovery 均被拒绝，且不会增加 Attempt、改变 Task/Call 或追加事件。
- 接管恢复改为按单个 `call_id` 原子处理；新 epoch 只能恢复更旧 epoch 的调用。一个并行调用异常不再扫描并误伤同 Run 的其他正常调用。
- Backend 外调前会在受 fencing 的事务内持久标记“可能已启动”；若此时发生 handoff，新 controller 会保守隔离旧 Session，避免把有歧义的调用当成确定未启动并复用同一 Session。升级前 `controller_epoch=NULL` 的活动调用也按 legacy unfenced 调用保守恢复，不会被永久跳过。
- `scheduler_tick()` 在等待长调用期间按 controller 租期的三分之一自动续租；1 秒 controller 租期配合 1.1 秒 Fake 调用已验证成功提交，不会因 tick 内租约过期丢失结果。
- 每个运行调用也在当前 controller epoch 下按 Assignment Lease 的三分之一自动续租；1 秒任务租约配合 1.1 秒 Fake 调用由原先的 `late` 修正为 `submitted`，Task、Attempt、Agent 和 Session 能正常收敛。
- `scheduler_tick()` 在控制权被其他实例持有时返回明确 `busy`，零派发、零 Attempt；一次性 tick 结束只使租约过期，不删除 controller 行，因此 epoch 保持单调。
- 这里完成的是 Orchestrator 运行控制权 fencing，不等同于业务层 Codex → CodeBuddy Supervisor Handoff；后者仍未实现。

## 第五切片：Task DAG、优先级和指数退避

- schema v8 增加 `task_dependencies`、依赖反向索引，以及每个 Task 的重试退避基数和上限；历史 Task 默认没有依赖，历史 dispatch spec 获得 `1s/60s` 默认值。
- `create_task_graph()` 在一个事务内校验并创建整张图；重复 Task、自依赖、缺失依赖、跨 Run 依赖和多节点环都会整批回滚。
- DAG Reconciler 只在全部上游 `COMPLETED` 后把 `PENDING` 推进到 `READY`；上游 `FAILED/CANCELLED` 会在同一事务内逐层传播，把所有下游稳定置为 `CANCELLED`。
- fan-in 已覆盖两个连接并发完成上游的情况：下游只产生一次 `PENDING→READY` 事件。人工绕过依赖直接置 `READY` 会被拒绝。
- Scheduler 在 claim 前执行 DAG reconcile；到期 Task 按 `priority DESC` 派发。尚未到期的高优先级 Task 不会阻塞已到期的低优先级 Task。
- retryable failure、超时和丢失 Worker 统一按 `min(max, base×2^(attempt_count-1))` 更新 `available_at`；同一 tick 不自旋重试，测试已验证 `2s→3s→3s` 封顶。
- 实际运行库已从 schema v6 升级至 v8；升级前备份为 `.agent-hub/state/agent-hub.db.v6-backup-20260831-stage2`。升级后仍为 10 Run、20 Task、29 Attempt、293 Event，`foreign_key_check=0`、`integrity_check=ok`。
- 实际历史库中的 `task_dependencies` 和 `run_controller_leases` 当前均为 0；本切片行为证据来自隔离临时数据库中的单元、并发和迁移测试，不把历史 Run 伪装成新架构实跑记录。
- 第五切片结束时共 101 项单元、契约和 Fake 集成测试全部通过；37 个 Python 文件通过无落盘编译检查，凭据特征扫描为 0。

## 第六切片：Pause / Resume / Cancel 与后台控制循环

- schema v9 增加 Run 级 `runs.control_state`（`RUNNING`/`PAUSED`）和 Task 级 `task_dispatch_specs.paused`，不引入新的 TaskState 状态、不破坏既有状态机。
- `pause_run/resume_run`、`pause_task/resume_task` 全部走 `BEGIN IMMEDIATE` + controller fencing + 审计事件；重复 pause/resume 幂等（同目标值直接返回，不重复写事件）。
- `claim_ready_dispatch()` 在事务内新增过滤：Run 为 `PAUSED` 或 Task dispatch spec 被 paused 的任务不派发，Pause 生效后一个调度周期内零新派发；Resume 只恢复原 READY/PENDING 任务，不绕过 DAG。
- `request_cancel_task()` 单事务按当前状态分派：PENDING/READY/REVIEW 直接 `CANCEL_REQUESTED → CANCELLED` 并级联 DAG 下游；ACTIVE 时持久 `CANCEL_REQUESTED`——`starting`（未 invoke）的调用零副作用直接取消，`running`（已 invoke）的调用标记 `cancel_requested` 并等待 interrupt；终态幂等 `noop`。`request_cancel_run()` 遍历 Run 内所有非终态 Task 同事务取消。
- `call_runtime.execute_adapter_call()` 在等待期间轮询持久 cancel 标志，发现后对 `RunningCall.cancel()` 发出 interrupt（50ms 轮询，远小于 10 秒 SLA）；confirmed 取消立即收敛，unconfirmed/unsupported 取消允许自然结束。
- `finish_backend_call()` 新增取消收敛分支：Task/Attempt 均为 `CANCEL_REQUESTED` 时，任何终态（含自然结束的 SUCCEEDED）都收敛到 `CANCELLED` 并释放 Lease/Agent/Session；SUCCEEDED 额外记为 `late_result=1`，绝不进入 REVIEW/INTEGRATION。
- 新增 `orchestrator/serve.py` 后台常驻循环：持有 Run controller 并后台续租（租期 1/3 间隔），每周期执行 reconcile_once → scheduler_tick → pool reconcile；续租遇 `FencedControllerError` 返回 `lost-controller` 并释放控制权；`stop_event`/`max_ticks` 明确停止，finally 释放 controller；重启后由 `recover_starting_calls` 接管恢复。
- CLI 新增 `serve`（`--run --interval --lease --max-ticks`）、`pause`、`resume`、`cancel`（`--run`/`--task`）子命令；控制命令采用 acquire → 操作 → release 模式，另一实例持有时返回明确 `busy`。
- 实际运行库已从 schema v8 升级至 v9；升级前备份为 `.agent-hub/state/agent-hub.db.v8-backup-20260831-stage2`。升级后仍为 10 Run、20 Task、29 Attempt、293 Event，`user_version=9`、`foreign_key_check=0`、`integrity_check=ok`；`runs.control_state` 与 `task_dispatch_specs.paused` 列已添加，历史 Run 默认 `RUNNING`/`paused=0`。
- 第六切片结束时共 116 项单元、契约和 Fake 集成测试全部通过（新增 15 项覆盖取消前、启动中、运行中 interrupt、完成竞态、重复取消、失权接管、Pause/Resume、Run 级取消、后台循环启停与 90 秒内过期租约回收）；无落盘编译通过，凭据特征扫描为 0。
- 环境适配：git 2.55.0 的 `worktree add -b` 对带斜杠分支名回归（`poc/worker` 报 `invalid reference`），`git_manager.py` 分支前缀改为 `poc-`（语义不变，阶段 1 测试不依赖分支名），基线恢复全绿后本切片才继续。

## 明确未完成

- 已有后台周期循环与过期租约回收（90 秒内），但还没有真实 Codex / CodeBuddy Adapter 接入统一 Scheduler；当前闭环只使用确定性 Fake Adapter。
- 还没有持久 Merge Queue / Outbox、进程重启后的 Git 对账和“不重复 merge”证明。
- 还没有预算门禁、人工审批命令和业务层 supervisor handoff（业务 `AuthorityLease` 未实现，不与 Run Controller lease 混淆）。
- 还没有正式的状态页 / 时间线 / 成本视图；CLI 控制命令已可人工执行。

下一切片应把已验证的 Codex / CodeBuddy PoC 调用适配到统一 Scheduler（S2-07），随后实现写范围串行化与持久 Merge Queue / Outbox（S2-08）。
