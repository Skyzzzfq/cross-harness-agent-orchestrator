# 阶段 1 PoC 进度报告

更新时间：2026-08-31

状态：**通过。阶段 1 全部退出条件已满足，阶段 2 尚未开始。**

## 已完成

- 用户已确认 `1 Codex supervisor + 2 CodeBuddy workers` 和“不自动付费”边界，阶段 0 决策为 GO。
- 新增版本化 Team / Role 配置，固定首版人数上限。
- 新增 Task 与 Attempt 状态机；非法捷径、终态复用会被确定性拒绝。
- 新增结构化 Message Envelope，要求收件人、关联 ID、序号和幂等键。
- 新增 SQLite WAL 状态库；规范化状态与追加事件在同一事务提交。
- 返工会保留旧 Attempt 的 `REJECTED` 终态，并创建编号递增的新 Attempt。
- 重复消息幂等键由数据库唯一约束阻断。
- 新增 `init` 和 `status` 命令；真实项目状态库已初始化到 `.agent-hub/state/agent-hub.db`。
- 新增 `demo --fake` 固定演示；两个 Worker 并行执行，A 一次通过，B 驳回后以新 Attempt 返工并通过。
- 新增受管 Git Workspace Manager：只允许写入自身创建的 worktree，拒绝 `.git`、绝对路径、`..` 越界和非受管目录。
- 新增 `demo --git-fake`：两个独立 worktree 中的 Worker 真实并行，提交串行集成；同路径冲突被检测、阻断并完整回滚。
- Git 子进程会清除可重定向仓库/索引的环境变量，禁用外部 hooks；无效 commit 和脏集成仓库作为错误处理，不伪装成普通冲突。
- checkout 保护同时核对 HEAD、完整 porcelain 状态和排除 `.git/.agent-hub` 后的工作区内容哈希。
- SQLite 状态库升级到 schema v2，增加 generation/fencing Assignment Lease；v1 数据可无损迁移。
- 新增 `demo --recovery-fake`：真实启动并杀死 Worker 子进程，旧 Attempt 进入 `STALE`；有额度时重排到 generation 2，无额度时 Task 明确进入 `FAILED`。
- 旧 generation 的心跳与晚到提交被拒绝；重复恢复幂等；事件追加失败时 Lease、Attempt、Task 整笔回滚。
- 新增真实 `demo --real` 路径：一个 Codex Thread 同时承担 Supervisor / Reviewer，两个 CodeBuddy Session 并行执行；B 的首次错误提交被真实审核驳回，新 Attempt / Session 返工。
- Codex 启动时强制 `forced_login_method=chatgpt` 并读取非敏感账号类型；验收记录确认 `chatgpt / plus`，不会回退到 API Key 认证。
- CodeBuddy Worker 仅允许 `StructuredOutput`，不获得文件或 Shell 工具；Adapter 将结构化结果写入唯一声明路径，Git Manager 再校验实际 diff 与精确字节。
- Reviewer 从不可变 result commit 读取真实 blob，并记录 commit、blob OID、SHA-256、审核标准版本和 Turn ID。
- 每个真实 Run 持久化 9 条派工、提交和审核消息；成功判定要求固定的 22 项检查全部通过。
- 真实验收历史采用追加记录；失败会中断连续计数，不能删除失败后挑选成功样本。

## 验证结果

- 单元测试：43 项通过。
- 状态机契约：13 条合法 Task 迁移、12 条合法 Attempt 迁移，以及代表性非法迁移全部匹配预期；已超过“至少 20 个 Fake / Adapter 契约场景”的数量要求，但阶段末还需汇总完整契约套件。
- 项目 Fake 演示结果：1 Run、2 个 `COMPLETED` Task、2 个 `ACCEPTED` Attempt、1 个保留的 `REJECTED` Attempt、3 条结构化消息和 31 条审计事件。
- 两个 Fake Worker 的执行区间真实重叠约 0.093 秒。
- 最新 Git Fake 演示中，两个 worktree Worker 重叠约 0.156 秒；两条正常提交通过确定性验证并成功集成，同路径冲突被阻断。
- Git Fake 演示前后，用户 checkout 的 HEAD、porcelain 状态和内容哈希均完全一致。
- Recovery Fake 演示中两个真实子进程均在运行时被强制终止且无残留；generation 1 失效、generation 2 完成，另一个任务按 `max_attempts=1` 进入 `FAILED`。
- 固定真实场景在两次如实记录的调试失败后，连续完整成功 3 次：`run-real-737061bb7d8d`、`run-real-1c7f2f8e3a64`、`run-real-fbfdd4d450fb`。最后一轮返回 `stage_ready=true`。
- 三条成功样本的双 Worker 重叠时间分别约 4.281、3.109、4.719 秒；每轮都完成 A 通过、B 首次驳回、B 第二次通过和仅接受提交集成。
- 每轮用户 checkout 的 HEAD、porcelain 状态和内容哈希均保持不变；报告、受管仓库与状态库扫描未发现明文密钥。
- 真实 CodeBuddy CLI 固定为 2.142.0，并在每次调用记录 CLI 路径、SHA-256、Session、耗时和厂商返回的用量字段；三连跑的 `total_cost_usd` 均为 0，但该字段不被解释为长期免费承诺。

## 阶段 1 签字结论

阶段 1 的八项退出条件全部满足：43 项测试覆盖超过 20 个 Fake / Adapter 契约场景；真实固定场景连续成功三次；双 Worker 重叠、真实返工、Worker kill/recovery、同路径冲突阻断、checkout 不变和无明文密钥均有持久证据。允许进入阶段 2，但本轮没有提前实现阶段 2 功能。

真实报告：`.agent-hub/reports/run-real-737061bb7d8d.json`、`.agent-hub/reports/run-real-1c7f2f8e3a64.json`、`.agent-hub/reports/run-real-fbfdd4d450fb.json`。追加历史：`.agent-hub/state/real-poc-history.jsonl`。

## 已知后续项

第一次真实调试失败发生在两个 Worker 已进入 `RUNNING` 之后，因此全局历史库曾保留 2 个 `ACTIVE` Task / `RUNNING` Attempt；它们不属于三条通过样本，也没有提交或集成结果。本报告没有删除或改写失败证据。阶段 2 第一切片随后通过 Reconciler 将两个 Attempt 原子回收为 `STALE`：A 因重试耗尽进入 `FAILED`，B 回到 `READY`；相关事件继续追加在同一历史 Run 中。

说明：PoC worktree 暂存于 `.agent-hub/poc-git/`，符合当前项目的运行时目录约束；MVP 再冻结 Windows 项目外短路径策略与自动回收周期。当前保留 worktree 和分支作为故障证据，不自动删除。
