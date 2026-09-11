# 个人开发版整改报告

## R0：冻结基线与能力实测

更新时间：2026-09-11  
状态：**R0、R1 已完成；整体整改保持 `stage3: checkpoint`，R2–R8 尚未开始。**

本报告是 `docs/PERSONAL_EDITION_REMEDIATION_PLAN.md` 的实施证据，不改写阶段 0–3 的历史签字。R0 只做基线、备份、能力实测和失败样本归档，没有重放或修改活动 Run，也没有把 SDK 的接口表面误写成编排器已经支持的行为。

## 1. 基线快照

| 项目 | R0 证据 |
|---|---|
| Git 分支 | `main` |
| Git HEAD | `e61d80fd930b0f4a28e0e4d8889f1f0f817c81c9` |
| 数据库 | `.agent-hub/state/agent-hub.db` |
| SQLite schema | v15 |
| 完整性 | `PRAGMA integrity_check = ok`；`foreign_key_check` 错误数为 0 |
| 数据库快照 | `.agent-hub/personal-edition/r0/agent-hub-20260911-172626.db`（通过 `orchestrator.db_ops.backup_database` 在线备份） |
| 恢复演练 | `.agent-hub/personal-edition/r0/restore-drill.db`；`db-restore` 和 `db-verify` 均为 schema v15、完整性 `ok`、外键错误 0；备份与恢复库 SHA-256 均为 `E2EF821D5B2F5DF24BD05BC8EF70AE58F94034D053C814C9701299189409107F` |
| 测试解释器 | `.venv\Scripts\python.exe`，Python 3.12.14 |
| 测试命令 | `.venv\Scripts\python.exe -m unittest discover -s tests` |
| 测试结果 | `Ran 311 tests in 118.441s`；`OK (skipped=1)`，即 310 通过、1 跳过 |
| HTTP 运行探针 | `GET http://127.0.0.1:8083/api/status` 返回 `serve: {}`；不据此修改数据库里的 Run 标记 |

### 1.1 既有工作区改动

R0 开始时工作区已非 clean。以下清单只作保护记录，未执行 reset、clean、覆盖或批量提交：

```text
M PROJECT_PROGRESS.md
M README.md
M docs/CONSOLE_GUIDE.md
M orchestrator/adapters/codebuddy_config.py
M orchestrator/adapters/contracts.py
M orchestrator/adapters/real.py
M orchestrator/agent_pool.py
M orchestrator/auth.py
M orchestrator/cli.py
M orchestrator/console/serve_manager.py
M orchestrator/console/server.py
M orchestrator/console/settings.py
M orchestrator/console/static/index.html
M orchestrator/core/config.py
M orchestrator/serve.py
M orchestrator/storage/sqlite_store.py
M orchestrator/workspace/git_manager.py
M start.cmd
M tests/test_probes.py
M tests/test_stage2_real_adapter.py
M tests/test_stage3_backend_routing.py
M tests/test_stage3_console.py
?? STAGE3_SUPERVISOR_ENTRY_REPORT.md
?? SUPERVISOR_ENTRY_HANDOFF.md
?? docs/CHAT_WORKSPACE_PLAN.md
?? docs/PERSONAL_EDITION_REMEDIATION_PLAN.md
?? orchestrator/console/login_flow.py
?? orchestrator/console/model_catalog.py
?? orchestrator/core/supervisor_plan.py
?? orchestrator/core/supervisor_planning.py
?? tests/test_login_flow.py
?? tests/test_supervisor_planning.py
?? tmp/
```

数据库快照显示 22 个历史/当前 Run 的 `control_state` 为 `RUNNING`，并存在 8 条控制器租约记录；R0 不把这些数据库标记等同于仍有后台进程，不接管、不清理、不重放。Run 6 的原始事件保留在活动库，脱敏后的回归样本见 [`docs/fixtures/run-6-plan-rejected.json`](fixtures/run-6-plan-rejected.json)。

## 2. SDK / CLI 能力表

版本来源和探测均不读取或输出凭据：

- Codex Python SDK / CLI：`openai-codex 0.147.0`，`codex-cli 0.147.0`。
- CodeBuddy Python SDK：`codebuddy-agent-sdk 0.3.248`；项目固定 CLI：`2.142.0`。

“SDK 表面”表示已通过本地导入、签名或 CLI help 观察到；“当前 Adapter”表示本仓库实际会调用并持久化的能力。两者有差距时，以下表格明确写出，不做推定。

| 能力 | Codex | CodeBuddy | 当前结论 / 后续 |
|---|---|---|---|
| 结构化输出 | SDK `AsyncThread.turn/run(..., output_schema=...)` 已存在；当前 Adapter `supports_structured_output=false`，尚未传 schema | CLI help 明确有 `--json-schema`；SDK 的 `extra_args` 可透传该参数，`ResultMessage.structured_output` 已存在；当前 Adapter 未传入 | **能力可用但编排未接入**；R1-4 接入并加 Fake/真实证据 |
| Session 恢复 | `AsyncCodex.thread_resume` 已存在；阶段 0 历史探针已验证同一 Thread 恢复 | `resume`、`continue_conversation`、`session_id` 和客户端历史读取均存在；阶段 0 历史探针已验证恢复 | SDK/历史实测通过；R3/R6 仍需把 provider session 与任务尝试绑定 |
| 流式文本 / 工具事件 | `AsyncTurnHandle.stream()` 已存在；当前 Adapter 只等待 `run()` 终态 | CLI/SDK 的 `stream-json`、`StreamEvent`、`include_partial_messages` 已存在；当前 Adapter 收集文本和终态，不落工具事件 | **接口存在、控制台未接入增量事件**；R6 |
| 运行中引导 | `AsyncTurnHandle.steer(input)` 已存在 | SDK 没有对应的 steer 方法；客户端可发送后续 query，但“活动 turn 中插入”未有可复现实测 | Codex 走 R5/R6；CodeBuddy 暂按“本轮结束后投递/新会话”降级，不宣称即时引导 |
| 取消确认 | `interrupt()` 返回 SDK 响应；当前 Adapter 会尝试 interrupt，并在不确定时标记 `backend_may_still_run` | `CodeBuddySDKClient.interrupt()` 存在但返回 `None`，没有确认响应；当前 Adapter 标记 `cancel_unconfirmed` 和 `backend_may_still_run=true` | Codex 需补真实终态证据；CodeBuddy 不能复用未确认资源；R5 |
| 读写边界 | `Sandbox.read_only/workspace_write` 已存在，当前 Adapter 按 access mode 选择 | CLI permission mode `plan/acceptEdits` 已存在，当前 Adapter 按 access mode 选择 | 这是后端声明/运行配置，不等于同 Windows 用户下的硬隔离；R2 做实际 diff、路径和 symlink 验证 |
| Usage | `TurnResult.usage`、耗时和 turns 字段已存在；当前 Adapter 映射，但 token 可能为空 | `ResultMessage.usage`、`total_cost_usd` 已存在；当前 Adapter 目前只保存 duration/turns | R5 只使用权威可得字段，未知不写成 0 |

### 2.1 能力闸门结论

R0 没有发现“必须停止整改”的 SDK 缺失：Codex 可提供结构化计划和 steer；CodeBuddy 的结构化输出可通过 CLI 参数降级接入，不能即时 steer/确认取消的限制已明确记录。CodeBuddy 的限制不阻塞 R1 的契约实现，但会阻止后续把它描述为“已验证即时引导”或“已确认取消”。

## 3. 模型成功与业务失败分层

Run 6 的主管调用本身是成功的：

- `backend=codex`、`state=succeeded`、`backend_invoked=true`；
- 返回文本是 `worker 1: 1` 与 `worker 2: 2`，但没有合法计划 JSON；
- 本地计划解析产生 `PlanValidationError: supervisor text is not valid JSON`，事件为 `plan.rejected`，任务仍停在 `REVIEW`。

因此这是“模型/后端调用成功，业务计划校验失败”，不是登录失败、连接失败或 Worker 已经完成。回归样本已保存为 [`docs/fixtures/run-6-plan-rejected.json`](fixtures/run-6-plan-rejected.json)。R1 必须在此基础上实现一次格式修复、版本化 revision 和批准前零 Worker 派发；不得自动重放旧 Run 6。

## 4. R0 出口核对

| 出口条件 | 结果 | 证据 |
|---|---|---|
| 备份可恢复且不覆盖活动库 | PASS | 在线备份 + 独立 `restore-drill.db` + 两次 `db-verify` |
| 基线可重现 | PASS | 固定解释器、完整 unittest 命令、311/310/1 结果 |
| 能力表没有“推定支持” | PASS | SDK 表面、当前 Adapter、历史实测、未验证项分列 |
| 每个缺口有后续切片 | PASS | R1 结构化计划/批准，R2 隔离，R3 会话/移交，R5 取消/预算，R6 对话/引导 |
| 旧 Run 未被迁移或重放 | PASS | 仅查询活动库和导出样本；未执行控制、任务或合并操作 |

## R1：角色模板、计划协议与批准状态

更新时间：2026-09-11  
状态：**R1 已完成（Fake 证据通过；真实 Codex 计划生成尚未在本环境重跑，记录为未验证而非成功）。**

### 1. 已实现

- `orchestrator/core/role_registry.py` 提供 supervisor、worker/implementation-worker、reviewer 三份版本化模板、角色绑定阻塞检查、团队能力目录和确定性 prompt builder；旧 schema-v1 team 文件缺少职责字段时自动补兼容模板。
- `RoleSpec` 保存职责、禁止动作、输入/输出契约、工具/上下文策略和预算默认值；`to_dict()` 用于 Run 快照。
- 计划协议兼容 v1，同时支持 v2 的 `revision`、`worker_concurrency`、`task_cap`、权限/预算、验收项、输入引用、任务类型、输出契约、模型/提供方约束；任务数量上限不再等同于 Worker 槽位。
- schema v16 新增 Run 的团队快照、快照摘要、批准模式和 `supervisor_plans` revision 表。直接调用旧 API 保持 auto 兼容；控制台新建 Run 默认 `manual`，批准模式在创建时持久化。
- 主管计划先校验/预览，批准绑定 revision、规范化摘要和权限范围；重复批准幂等，摘要或权限不匹配拒绝。批准前不会创建 Worker。
- 计划格式失败保留原始 `plan.rejected`，最多安排一次新的主管尝试；第二次失败写入 `plan.needs_input`，不派发 Worker。旧 rejected 事件不阻塞后续新 call/revision。
- 控制台增加 `GET /api/runs/{run_id}/plans`，以及 `POST /api/runs/{run_id}/plans/{task_id}/approve|reject`。

### 2. R1 验证

| 检查 | 结果 | 证据 |
|---|---|---|
| v2 六任务、两个并发槽 | PASS | `tests/test_personal_r1.py::R1ContractTests.test_two_slots_allow_six_sequential_tasks` |
| manual 预览、批准前零子任务、批准幂等 | PASS | `tests/test_personal_r1.py::R1ApprovalTests.test_manual_plan_is_previewed_then_approved_once` |
| 旧 supervisor 规划/serve 行为兼容 | PASS | `tests/test_supervisor_planning.py`（16 项） |
| 角色 prompt 与旧 team 兼容 | PASS | `tests/test_personal_r1.py::R1ContractTests.test_legacy_role_gets_template_and_prompt` |
| 全量回归 | PASS | `.venv\\Scripts\\python.exe -m unittest discover -s tests`；314 项，313 通过、1 跳过 |
| 真实 Codex 合法计划 | **未验证** | 本轮未消耗真实后端额度；不得把 Fake 结果写成真实成功。 |

### 3. R1 出口与限制

Fake 主管可以生成 v2 计划、在人工批准前保持零 Worker 派发、批准后原子物化并支持重复点击。真实 Codex/CodeBuddy 计划生成仍需在有可用登录态的本机执行一次；当前只保留明确的“未验证”状态。R2 可在此契约基线上继续，但不能宣称真实端到端已通过。

## 5. 下一步

R1 已完成，下一切片是 R2：实现 Run 四区 manifest、task/attempt worktree、scratch/只读资源快照、artifact manifest 和实际 diff/write_scope 校验。整体状态仍是 `stage3: checkpoint`，不是个人版最终验收。
