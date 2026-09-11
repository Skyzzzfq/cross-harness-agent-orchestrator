# 主管任务入口：单指令自动拆解交接说明

更新时间：2026-09-10  
交接基线：`e61d80f`（`main`）  
交接对象：下一轮负责实现“只给主管一个总任务”的 Agent

## 1. 目标

把当前已经存在的 3-Agent 编排能力产品化为一个通用入口：

```text
用户只输入一次总任务
        ↓
Codex supervisor 负责规划
        ↓
系统校验并创建子任务
        ↓
CodeBuddy worker A / worker B 并行执行
        ↓
REVIEW、返工、批准和 Git 集成
```

目标团队仍然是默认的：

```text
1 × Codex supervisor
2 × CodeBuddy worker
```

本交接的重点不是重新实现 Agent Pool、调度器或 Git 合并，而是补齐“主管任务入口”和“计划转任务图”的产品闭环。

## 2. 当前已经完成的能力

接手前必须先阅读：

1. `AGENTS.md`
2. `PROJECT_PROGRESS.md`
3. `跨Harness多Agent团队编排系统实施计划.md`
4. `docs/WRITE_LOOP_UI_REPORT.md`
5. `config/team.yaml`
6. `orchestrator/console/server.py`
7. `orchestrator/serve.py`
8. `orchestrator/scheduler.py`
9. `orchestrator/storage/sqlite_store.py` 中的 `create_task`、`create_task_graph`、`claim_ready_dispatch`

当前基线已经具备：

- Team / Role / Agent Pool 配置。
- 默认 `1 Codex + 2 CodeBuddy` 团队。
- 按 `required_role_id` 和 `required_backend` 路由任务。
- 两个不同 Session 的并行执行。
- Task、Attempt、Session、Assignment Lease 和 Run Controller。
- authority epoch / controller fencing。
- Task DAG、依赖、暂停、取消、重试和恢复。
- 受管 worktree、`write_scope` 隔离、REVIEW、返工、Git 合并和 Outbox。
- 本地网页控制台、Run 管理、团队配置和写任务审核闭环。
- 真实场景已验证 Codex 规划/审核 + 两个 CodeBuddy 并行执行。
- 当前最新写任务报告为 266/266 测试通过；以 `PROJECT_PROGRESS.md` 和实际测试结果为准。

已有固定真实验收流程可参考：

- `orchestrator/poc/real_demo.py`
- `python -m orchestrator demo --real`

注意：固定 `demo --real` 已经能完成主管规划，但它不是通用网页入口。本任务是把同类能力接入普通 Run 和网页控制台。

## 3. 当前缺口

现在网页创建任务的默认值是 `required_role_id="worker"`。因此普通使用路径更接近：

```text
用户 → 创建 worker 任务 A
用户 → 创建 worker 任务 B
```

还没有完成：

```text
用户 → 创建一个 supervisor 任务
Codex → 返回结构化计划
Hub → 自动创建 worker 子任务
```

不要把这个缺口误认为 Agent Pool 或并行调度未完成；缺的是通用的“规划结果解析、任务图创建、幂等和 UI 入口”。

## 4. 建议的最小实现范围

### 4.1 主管任务入口

在现有任务创建接口基础上增加一种明确的任务模式，例如：

```json
{
  "mode": "supervisor",
  "prompt": "检查项目并拆成两个互不冲突的实现任务",
  "access_mode": "read_only",
  "required_role_id": "supervisor"
}
```

具体字段名可以调整，但必须满足：

- 主管任务明确路由到 `supervisor` role。
- 主管规划任务默认只读，不得直接修改项目文件。
- 不要绕过现有 controller、authority、budget 和 workspace 校验。
- 不能让普通 worker 任务误被 Codex supervisor 领取。

可以采用新的专用接口，也可以扩展现有 `POST /api/runs/{run_id}/tasks`；优先选择改动小、能复用现有状态机的方案。

### 4.2 结构化计划契约

主管必须返回严格 JSON，不接受自由文本猜测。建议最小契约如下：

```json
{
  "plan_version": 1,
  "summary": "一句话说明计划",
  "tasks": [
    {
      "task_id": "worker-a",
      "role_id": "worker",
      "backend": "codebuddy",
      "prompt": "具体执行说明",
      "access_mode": "write",
      "write_scope": ["demo/a.txt"],
      "depends_on": []
    },
    {
      "task_id": "worker-b",
      "role_id": "worker",
      "backend": "codebuddy",
      "prompt": "具体执行说明",
      "access_mode": "write",
      "write_scope": ["demo/b.txt"],
      "depends_on": []
    }
  ]
}
```

计划校验至少包括：

- 必须是 JSON 对象，`plan_version` 必须受支持。
- `tasks` 非空且数量不得超过团队允许的并发上限。
- `task_id` 在本次计划内唯一，不能覆盖已有任务。
- `role_id` 只能引用已声明的 Role；普通子任务默认应为 `worker`。
- `backend` 只能使用 Team 中存在的 backend pool。
- `prompt` 非空，不能包含凭据或未脱敏的敏感信息。
- 写任务必须有非空、相对、无 `..`、不包含 `.git` 的 `write_scope`。
- 通过现有 `WorkspacePolicy` 校验 cwd 和 scope。
- 写 scope 之间必须不冲突；冲突计划要拒绝或要求返工，不能静默并行。
- `depends_on` 只能引用本计划中的任务，不能形成环。
- 忽略未知字段，还是拒绝未知字段，必须选定一种并写测试；安全上推荐拒绝未知字段。

不要直接信任模型返回的 `backend`、`role_id`、`cwd` 或 `write_scope`。模型只是提出计划，Hub 才是最终权限和路径裁决者。

### 4.3 计划落库与幂等

计划解析和子任务创建必须具备幂等性：

- 同一个主管结果重试时，不得重复创建 worker 任务。
- 建议使用主管 attempt/generation + 计划摘要哈希作为幂等键。
- 计划落库、子任务图创建和审计事件必须在同一事务内完成，或使用现有可恢复 Outbox 方案。
- 如果计划非法，不能产生半套子任务。
- 主管成功但子任务创建失败时，要有明确失败事件和可重试状态。
- 旧 authority/controller epoch 的晚到计划必须零副作用。

优先复用 `create_task_graph` 和现有事件/审计机制，不要新建第二套任务状态机。

### 4.4 父任务和子任务关系

先采用最小方案：主管任务作为“规划任务”，worker 任务作为独立子任务，并用现有 DAG/依赖和事件记录关联关系。

如果现有 schema 没有父子字段，不要为了 UI 方便立刻扩展大量表。可以先使用：

- 主管任务 ID；
- `plan.created` / `plan.rejected` / `plan.materialized` 事件；
- 子任务 prompt 中的受控 `parent_task_id` 元数据；
- 或现有 DAG 依赖关系。

只有在现有审计和查询无法可靠关联时，才新增最小 schema 变更，并补迁移、备份/恢复和回滚测试。

### 4.5 网页控制台

在当前“发起任务”区域增加最小 UI：

- 任务类型：`普通任务` / `主管任务`。
- 主管任务模式下隐藏或锁定 `required_role_id=supervisor`。
- 明确提示：主管只负责规划，实际代码修改由 worker 在受管 worktree 中完成。
- 展示主管规划状态：规划中、计划已生成、计划被拒绝、子任务执行中、等待审核、已完成。
- 展示由主管生成的子任务列表、角色、backend、write_scope 和依赖。
- 允许用户在派发前批准计划或取消计划；如果本轮先不做“计划前审批”，必须明确记录为后续增强项。
- 保留现有取消、暂停、恢复、REVIEW 通过/打回和事件时间线。

最小版本可以先只支持：一个主管任务生成两个互不冲突的 worker 写任务。不要同时加入自由拓扑编辑、无限递归主管、跨 Run 委派或团队自动扩缩容。

## 5. 验收标准

完成后至少满足以下条件：

### A. 离线 Fake 验收

1. 一个 supervisor 任务能被唯一的 supervisor Agent 领取。
2. supervisor 返回两个合法 worker 任务。
3. 两个 worker 任务自动落库并进入 READY。
4. 两个 worker 使用不同 Agent/Session 并发生真实并行重叠。
5. 两个 worker 完成后进入现有 REVIEW 流程。
6. 通过审核后，两个不冲突写任务都能真实 Git 集成并进入 COMPLETED。
7. 主管计划只执行一次，重复 tick/重复回调不会产生重复子任务。

### B. 失败与安全验收

1. supervisor 返回非法 JSON：不创建任何子任务，并产生可定位的失败事件。
2. 计划引用未知 role/backend：拒绝，零副作用。
3. 计划包含绝对路径、`..`、`.git`、项目外 cwd 或 scope 冲突：拒绝，零副作用。
4. 计划任务数量超过并发/预算：拒绝或截断到明确安全策略，不能超发。
5. 主管超时、取消或失败：不产生未授权子任务。
6. 旧 authority epoch 或旧 controller 的迟到计划：零副作用。
7. 计划文本中出现 API key、cookie、session token 等敏感内容：持久化前统一脱敏。
8. 重试、进程崩溃或重复提交后：不得重复创建任务、重复 merge 或虚假 COMPLETED。

### C. 网页验收

1. 只在网页输入一次总任务。
2. 页面显示 Codex supervisor 正在规划。
3. 页面显示两个 worker 子任务已经生成并分别派发。
4. 页面可以看到两个 worker 的并行状态和事件时间线。
5. REVIEW、打回、重新派发、通过和 Git 集成仍然可用。

### D. 回归验收

- 现有全量测试保持通过；当前基线报告为 266 项，实际数量以测试运行结果为准。
- 运行新增专项测试和全量测试。
- 编译检查、凭据扫描、数据库 integrity_check 和 foreign_key_check 通过。
- 不改变现有 authority fencing、workspace boundary、merge/outbox 原子性和写任务安全边界。

## 6. 推荐测试文件和新增测试名称

优先在以下位置增加测试：

- `tests/test_stage3_console.py`：主管任务入口和网页 API。
- `tests/test_stage3_backend_routing.py`：supervisor/worker 路由隔离。
- `tests/test_stage2_agent_runtime.py` 或新的 `tests/test_supervisor_planning.py`：计划物化、并行和幂等。
- `tests/test_stage3_approval.py`：计划批准、取消和审核链。
- `tests/test_stage3_windows.py`：Windows 路径和 scope 边界。

建议至少覆盖这些测试名对应的行为：

```text
test_supervisor_task_routes_only_to_supervisor
test_valid_plan_materializes_two_worker_tasks
test_plan_materialization_is_idempotent
test_invalid_plan_creates_no_children
test_unknown_role_or_backend_is_rejected
test_plan_scope_escape_is_rejected
test_conflicting_worker_scopes_are_rejected
test_supervisor_cancel_does_not_spawn_children
test_stale_epoch_plan_has_zero_side_effects
test_two_materialized_workers_run_in_parallel
test_web_console_can_create_supervisor_task
```

## 7. 推荐实现顺序

按小切片推进，不要一次重写控制台：

1. **Slice A：计划契约与纯校验器**
   - 新增独立、无副作用的计划解析/校验函数。
   - 先补失败测试，再补实现。

2. **Slice B：主管结果转 Task DAG**
   - 复用 `create_task_graph`。
   - 加幂等键、计划事件和失败回滚。

3. **Slice C：调度器接入**
   - supervisor 任务执行结束后触发计划物化。
   - 只在当前 epoch、当前 generation 和合法终态下触发。

4. **Slice D：Fake 全流程**
   - 一个主管 + 两个 worker，验证并行、审核、返工和集成。

5. **Slice E：网页入口**
   - 增加任务类型、计划状态、子任务展示和错误提示。

6. **Slice F：真实 Codex 主管验证**
   - 使用 ChatGPT Plus saved login，不要求 OpenAI API Key。
   - CodeBuddy 使用中国站登录态。
   - 先只读规划，再开放写任务物化；不要一开始让主管直接写代码。

每个 Slice 单独测试、单独提交，并同步更新 `PROJECT_PROGRESS.md` 和适用阶段报告。

## 8. 不要做的事情

- 不要把主管规划交给用户手工复制粘贴到两个 worker。
- 不要让模型自行决定最终权限、路径、预算或 merge 权限。
- 不要绕过 `WorkspacePolicy`、authority/controller fencing 或现有审核链。
- 不要增加第二套 Task/Attempt 状态机。
- 不要默认让多个 Agent 修改同一文件。
- 不要把 token、cookie、session secret 写入 prompt、事件、报告或 Git。
- 不要把 `demo --real` 的固定脚本直接当成通用产品逻辑。
- 不要因为 UI 需要而删除失败证据或历史事件。
- 不要未经确认引入大型前端框架或替换 Python 编排核心。

## 9. 本地验证命令

接手后先执行：

```powershell
& '.venv\Scripts\python.exe' -m unittest discover -s tests -v
& '.venv\Scripts\python.exe' -m orchestrator status
git status --short
git log --oneline --decorate -15
```

离线验证可使用：

```powershell
& '.venv\Scripts\python.exe' -m orchestrator demo --fake
```

真实验证前必须确认已登录且知悉会消耗账号额度：

```powershell
& '.venv\Scripts\python.exe' -m orchestrator demo --real
```

## 10. 提交和交接纪律

- 行为变化前先补失败测试。
- 每个 Slice 完成后运行新增测试和全量测试。
- 未完成全部阶段退出条件时只能使用：

```text
stage3: checkpoint supervisor entry <short description>
```

- 不得使用 `stage3: complete`，除非阶段 3 的所有退出条件重新核验通过。
- 提交前检查 `git diff --check`、凭据扫描和 `.agent-hub/` 是否未被纳入 Git。
- 更新 `PROJECT_PROGRESS.md` 时保留历史审计结论，不得静默删除旧问题。
- 交接时报告：实现的 Slice、测试数量、未完成项、已知限制、最新 commit SHA。

## 11. 完成定义

只有同时满足以下条件，才可以声称“用户只需给主管一个指令”：

1. 普通网页 Run 能创建 supervisor 任务。
2. supervisor 真实返回结构化计划。
3. Hub 自动校验并生成两个 worker 子任务。
4. 两个 worker 自动并行执行，不需要用户再次复制任务指令。
5. 审核、返工、批准、Git 集成和失败恢复沿用现有闭环。
6. 非法计划、越界路径、旧 epoch、重复回调均零副作用。
7. Fake、网页和至少一次真实 Codex supervisor 场景均有证据。

在此之前，对外只能表述为：

> 当前已具备 3-Agent 并行编排内核和固定主管验收流程；通用网页主管入口仍在开发中。

