# 阶段 2 流程与安全审计问题清单

审计日期：2026-09-01  
审计方式：只读交叉核验项目计划、阶段报告、交接文档、实现代码、测试、运行数据库、真实运行报告和 GitHub 远端状态。  
用途：交给 WorkBuddy 复核并制定修复方案。本文不等同于修复完成或新的阶段签字。

## 1. 给 WorkBuddy 的直接任务

请先复核本文每一项，不要直接开始阶段 3，也不要删除或改写现有失败/运行证据。

需要输出：

1. 对每项问题给出“确认 / 部分确认 / 不成立”的判断和代码证据。
2. 对确认的问题给出最小修复方案、失败测试和预计提交顺序。
3. 优先修复 P0，再修复 P1；每次行为变化后运行全量测试。
4. 修复前把阶段 2 视为 `checkpoint / audit-open`，不能仅凭现有 150 项测试继续进入阶段 3。
5. 修复后重新执行完整的真实端到端退出矩阵，并更新 `STAGE2_REPORT.md`、`PROJECT_PROGRESS.md` 和本文件。

## 2. 已核验的健康基线

- 当前提交：`5e2cc2e`（GitHub `main` 已验证为同一 SHA）。
- 全量测试：150/150 通过。
- 当前数据库：schema v12。
- SQLite：`integrity_check=ok`，`foreign_key_check=0`。
- 真实 Adapter 报告存在：10 个冻结只读场景均返回匹配 marker；2 CodeBuddy + 1 Codex 的执行时间存在真实重叠。
- `pip check` 未发现依赖冲突。
- 上述健康项不能覆盖本文所列的权限、事务边界和端到端证据缺口。

## 3. P0：必须先处理的问题

### P0-01：业务 AuthorityLease 没有约束派发和集成

证据：

- `orchestrator/core/models.py` 中 `AuthorityToken` 的说明声称它保护派发、审核和集成。
- `orchestrator/storage/sqlite_store.py::claim_ready_dispatch()` 只接收 `ControllerToken`，不接收 `AuthorityToken`。
- `enqueue_merge()`、`claim_merge_queue()`、`finish_merge()` 同样只校验 Run Controller。
- `acquire_authority()` 在已有 ACTIVE AuthorityLease 时，任何调用者都可以直接替换 owner 并递增 epoch；不要求旧 authority token、租约过期、已接受 handoff 或人工强制接管审批。
- 现有 authority 测试主要证明旧 token 无法续租或记录 review decision，没有证明旧主管无法派发和集成。

影响：

- 业务主管移交后，旧主管 fencing 对真实派发和集成没有约束力。
- 持有运行控制器的进程可以绕过业务主管权。
- 阶段计划中“旧主管的派发、审核和集成命令被拒绝”的退出条件未被实现。

建议验收：

- 所有派发、审核、入队集成、领取集成和完成集成入口都必须携带并原子校验当前 `AuthorityToken`。
- 旧 authority epoch 对上述每个入口均为零副作用：不新增 Attempt、Backend Call、Merge Queue、Event 或 Git 变化。
- ACTIVE authority 不得被无条件覆盖；强制接管必须是单独的人工审计流程。
- 增加 Codex → CodeBuddy handoff 的完整测试：新主管向 Codex 派发、审核、集成成功；旧主管三个动作全部失败。

### P0-02：Merge Queue、Git 和 Outbox 没有形成原子闭环

证据：

- `enqueue_merge()` 不验证 Task 是否处于 REVIEW、Attempt 是否存在并已 ACCEPTED、三层审核是否通过、result commit 是否存在或是否属于受管 worktree。
- 现有测试可以使用不存在的 `attempt-1` 和任意字符串 `abc123` 入队。
- `finish_merge(..., "applied")` 可直接将 Task 推进至 COMPLETED，但该方法没有执行或验证真实 Git 集成。
- `result_commit` 参数没有用于证明 Git HEAD 已包含对应补丁。
- `record_outbox_intent()` 和 `finish_merge()` 是两个分别提交的事务；测试也分两次调用，因此不是 Transactional Outbox。
- `serve()` 和 CLI 常驻循环没有消费 Merge Queue、执行 Git integrate 或投递 Outbox。
- `finish_merge()` 没有严格要求当前状态为 APPLYING，重复调用可能再次写入状态或事件。

影响：

- 数据库可能显示 COMPLETED，但 Git 没有相应提交。
- Git 已落地后进程崩溃，数据库可能仍为 APPLYING/PENDING。
- Outbox 可能先声称成功，业务状态却没有成功；也可能业务状态成功但没有 Outbox。
- “重启后不重复副作用、不重复 merge”的证明只覆盖了孤立辅助方法，没有覆盖真实常驻流程。

建议验收：

- 入队前原子验证 Task、Attempt、审核链、不可变 result commit 和 authority epoch。
- 外部 Git 操作采用明确的 intent → execute → reconcile 流程；数据库只能在 Git 对账成功后标记 APPLIED/COMPLETED。
- merge 业务状态变化与 Outbox intent 必须在同一 SQLite 事务提交。
- 崩溃点至少覆盖：入队前、Git 前、Git 后数据库前、数据库后 Outbox 前、Outbox 投递后确认前。
- 每个崩溃点重启后均满足：0 丢 merge、0 重复 merge、0 虚假 COMPLETED、0 重复外部通知。

### P0-03：cwd 和 write_scope 缺少统一的规范化边界

证据：

- `AccessPolicy.__post_init__()` 只要求 `cwd` 非空，没有校验其属于项目根目录或受管 worktree。
- `create_task()` / `create_task_graph()` 可保存任意 cwd。
- Windows 下 `codex_transport_environment()` 会在传入 cwd 下创建 `.agent-hub/certs/windows-roots.pem`；未经验证的 cwd 因此可能导致项目外写入。
- `write_scope` 冲突检测只使用字符串集合交集，未做绝对化、大小写归一、`..` 消解、目录包含关系、symlink/junction 检查。
- access mode 为 write 时允许空 `write_scope`。

影响：

- 错误或恶意任务可以让 Adapter 读取项目外目录。
- Windows 证书初始化可能直接写入项目外路径。
- `src` 与 `src/app.py`、大小写变体、`a/../src/app.py` 等真实重叠可能被当成不冲突并行执行。

建议验收：

- 在 Task 创建和派发两个边界都 canonicalize 并验证 cwd。
- 写任务 cwd 必须是注册过的受管 worktree；只读任务也必须限制在显式允许根中。
- write_scope 必须是非空、相对、规范化、禁止 `..`/绝对路径/`.git`/symlink escape 的路径集合。
- Windows 比较使用大小写不敏感规则；目录 scope 必须与所有子路径冲突。
- 证书缓存固定放在项目自己的 `.agent-hub/`，不能由任务 cwd 决定。

## 4. P1：高优先级实现和证据缺口

### P1-01：真实“10/10 终态”实际只到 REVIEW

证据：

- `.agent-hub/reports/run-stage2-real-007520622d15.json` 的 10 个 Task 全部为 REVIEW，而不是 COMPLETED。
- `TaskState.REVIEW` 在状态机中仍可进入 READY、INTEGRATION、COMPLETED 或 CANCEL_REQUESTED，明确不是终态。
- `orchestrator/poc/stage2_real.py` 却把 REVIEW 计入 `scenarios_terminal`。
- 真实报告没有走三层审核、人工审批、Merge Queue、Git 集成、Outbox 和最终 commit 追踪。

影响：

- “真实 10/10 正确编排终态”的措辞不准确。
- 真实 Adapter 只证明了调用和提交到 REVIEW，不能证明完整 MVP 闭环。

建议验收：

- 将 Adapter 调用完成称为“到达 REVIEW”，不能称为 Task 终态。
- 新增至少 10 个真实完整场景，正确结果必须经过审核和集成进入 COMPLETED；失败、取消和冲突进入预声明终态。
- 报告中分别统计 adapter terminal、task terminal 和 full-pipeline terminal。

### P1-02：硬预算没有覆盖 turn、Token 和金额

证据：

- `budgets` 保存 `max_turns` 和 `max_cost_decimal`。
- `budget_status()` 只计算 calls、tasks 和 run_seconds。
- `UsageReport` 中的 turns、Token、cost 没有参与派发前硬门禁。
- 现有费用测试只核对 usage 数量，没有证明达到金额或 turn 上限后零新增调用。

建议验收：

- 聚合 authoritative usage；权威字段达到上限后零新增 Backend Call。
- 无权威 usage 时按计划规定使用清晰记录的保守估算。
- 并发派发必须预留预算，避免多个同时 claim 越过最后一个额度。

### P1-03：审批只是记录，未成为可消费的安全门禁

证据：

- `create_approval_request()` 不要求 AuthorityToken。
- `decide_approval()` 仅接受自由文本 `decided_by`，没有操作者认证边界。
- `expires_at`、`params_hash`、scope 和 Attempt 绑定未在危险动作执行前校验。
- schema 有 USED 状态，但没有消费审批并转为 USED 的实现。
- “single_use” 测试只证明不能重复决定，不是不能重复执行动作。
- CLI 有查看、批准、驳回，没有实施计划要求的重新分配命令。

建议验收：

- 增加原子 `consume_approval()`，核对 run/task/attempt/action/params hash/scope/expiry，并从 APPROVED 变为 USED。
- 所有危险动作必须在同一事务中消费指定审批；重复执行应零副作用。
- 明确本地人类身份信任模型，并记录审计来源。
- 实现并测试重新分配命令。

### P1-04：真实 Adapter 没有支持统一 Scheduler 的写任务

证据：

- Codex Adapter 固定 `Sandbox.read_only`。
- CodeBuddy Adapter 固定 `permission_mode="plan"`。
- 两者都没有按 `request.policy.access_mode` 选择受控写模式。
- CLI `serve` 当前只注册 Fake Adapter；真实 Adapter 仅由专用冻结场景命令运行。

影响：

- 真实 Agent 无法通过 Stage 2 常驻编排链路在受管 worktree 完成实现任务。
- GitWorkspaceManager 仍主要用于 PoC 和测试，没有接到 Scheduler → Review → Merge 主流程。

建议验收：

- 只允许写任务进入已注册的独立 worktree，并将 Adapter 权限限制到声明 write_scope。
- 常驻服务可配置 Fake/Codex/中国站 CodeBuddy Adapter，而不是写死 Fake。
- 增加真实小型写任务：两个不重叠任务并行、审核、集成，用户 checkout 指纹不变。

### P1-05：超时、取消和敏感错误信息仍有风险

证据：

- Codex 调用使用 `wait_for(shield(turn.run()))`；超时后底层 Turn 可能继续运行，但返回快照没有设置 `backend_may_still_run=true`，也没有确认 interrupt。
- CodeBuddy 取消停止本地消费任务，但厂商调用可能继续；没有持久 provider call handle 用于后续查询。
- Adapter 将 `str(exc)[:500]` 直接写入 Failure，Scheduler 会持久化 `failure_json`；Stage 2 路径没有统一的凭据/URL/query-string 脱敏。

建议验收：

- 超时必须走与取消相同的 interrupt/不确定性状态机。
- 无法确认终止时 Session 必须隔离，晚到结果不得审核或集成。
- 所有 SDK 异常、provider raw、URL 和日志在持久化前统一脱敏。

### P1-06：Outbox 失败后不会自动重试

证据：

- `claim_outbox()` 只领取 PENDING。
- `finish_outbox(..., "failed")` 把记录改为 FAILED。
- 没有把 FAILED 按退避策略重新置为 PENDING，也没有常驻投递器。

建议验收：

- 增加持久 claim lease、指数退避、最大重试次数和死信状态。
- 在 claim 后崩溃、投递成功但确认前崩溃等情况下保持幂等。

## 5. 流程记录不一致

### DOC-01：`WORKBUDDY_HANDOFF.md` 已过期

当前交接单仍写：

- schema v8、101 项测试；
- 从 S2-06 开始；
- 不得把阶段 2 标为完成；
- Stage 2 报告应为进行中。

而 `PROJECT_PROGRESS.md` 和 `STAGE2_REPORT.md` 已写 schema v12、150 项测试、Stage 2 完成。继续使用旧交接单会导致重复开发或错误覆盖。

### DOC-02：`PROJECT_PROGRESS.md` 自相矛盾

- 文件开头宣布 Stage 2 已完成。
- 中间标题仍是“阶段 2 尚未完成”。
- 开发纪律仍要求领取“阶段 2 尚未完成”中的第一项，但所有项目已打勾。

### DOC-03：完成提交和远端核验没有写入台账

- 阶段纪律要求在 `PROJECT_PROGRESS.md` 记录完成 commit SHA 和远端验证结果。
- 实际完成提交 `d2519fe`、文档修正提交 `5e2cc2e` 未写入台账。
- 本次审计已只读确认 GitHub `main` 为 `5e2cc2e`。

### DOC-04：Stage 2 的签字措辞超过现有证据

以下声明需要在修复和重新验收前降级或加注“仅组件测试”：

- 旧业务 epoch 不能派发、审核或集成。
- Transactional Outbox。
- 真实 10/10 正确终态。
- 人类可重新分配和使用一次性审批。
- turn、Token、金额硬预算。
- 所有最终 commit 可追溯到 Task、Agent、Role、消息、测试和审核。
- 无未处理高危或严重安全缺陷。

## 6. 供应链检查状态

- `pyproject.toml` 固定了两个直接依赖：`openai-codex==0.147.0`、`codebuddy-agent-sdk==0.3.248`。
- 没有提交依赖锁文件或哈希清单，间接依赖无法稳定复现。
- 当前环境未安装 `pip-audit`，本次没有完成自动化 Python CVE 全量扫描。
- OpenAI Codex 已公开的 GHSA-w5fx-fh39-j5rw 影响 0.2.0 至 0.38.0，0.39.0 修复；当前 0.147.0 不在该公告的影响范围内：
  https://github.com/openai/codex/security/advisories/GHSA-w5fx-fh39-j5rw
- 未找到足以证明 CodeBuddy SDK 当前版本“无漏洞”的官方公告，因此不能把搜索无结果当作安全证明。

建议：生成可复现锁文件、记录 wheel/hash、在 CI 中加入依赖审计和 secret scan，并明确告警处理规则。

## 7. 推荐修复顺序与提交计划

不得并行修改相互依赖的状态机核心；建议按以下顺序执行：

1. `stage2: checkpoint enforce authority and takeover fencing`
   - P0-01，先封住业务主管权限绕过。
2. `stage2: checkpoint canonical workspace and write scopes`
   - P0-03，建立所有真实写能力的路径前提。
3. `stage2: checkpoint transactional merge git and outbox`
   - P0-02 + P1-06，接通真实集成和恢复闭环。
4. `stage2: checkpoint approval and complete budget gates`
   - P1-02 + P1-03。
5. `stage2: checkpoint real writable scheduler and cancellation`
   - P1-04 + P1-05。
6. `stage2: checkpoint corrected exit matrix and handoff records`
   - 重跑 Fake/真实完整终态矩阵，修正文档。
7. 只有所有退出条件重新通过后，才创建新的 `stage2: complete ...` 签字提交。

## 8. WorkBuddy 需要逐项回答的问题

1. 是否确认 `AuthorityToken` 当前没有进入 dispatch/merge API？准备如何避免 controller 与 authority 职责再次混淆？
2. `acquire_authority()` 的无条件接管是有意设计还是实现遗漏？人工强制接管的信任边界是什么？
3. 如何保证 Git 已落地、数据库 APPLIED/COMPLETED 和 Outbox intent 三者在崩溃后最终一致？
4. 为什么现有真实场景把 REVIEW 记为 terminal？修复后正确终态集合是什么？
5. 如何把 cwd/write_scope 规范化为 Windows 安全的受管 worktree 边界？
6. 如何实现审批的 scope/params/expiry/single-use 原子消费？
7. authoritative usage 缺失时，Token/金额预算采用什么保守估算？
8. 常驻服务何时接入真实 Codex/中国站 CodeBuddy、Review、Merge 和 Outbox？
9. Codex timeout 后仍可能运行的 Turn 如何 interrupt、隔离和恢复？
10. 修复后哪些测试能证明用户 checkout 始终不变、0 重复 merge、0 越界访问？

## 9. 复核命令

```powershell
& '.venv\Scripts\python.exe' -m unittest discover -s tests -v
& '.venv\Scripts\python.exe' -m orchestrator status
git status --short
git log --oneline --decorate -15
git ls-remote --heads origin main
```

修复完成时还应附带新的真实运行报告、数据库完整性结果、Git 对账证据和失败注入矩阵，不能只提供单元测试总数。
