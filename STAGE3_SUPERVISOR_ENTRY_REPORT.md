# Stage 3 Supervisor Entry Checkpoint

后续整改入口（2026-09-11）：`docs/PERSONAL_EDITION_REMEDIATION_PLAN.md` 已编制，R0–R8 尚未实施。个人版新 Run 采用计划默认人工批准、显式可选自动策略；角色契约、任务尝试级 worktree、独立证据审核和对话投递按该计划验收。本段仅记录计划，不新增实现或测试通过声明，以下内容保留为历史切片记录。

## 登录反馈修复

后续根因核验：用户网页登录已成功，但由 Codex 沙箱启动的控制台无法正确读取用户级凭据。同 SDK 沙箱内无法读取、批准后正常用户权限返回 True。正常权限后台启动 8091 后，控制台连接接口实测 `logged_in=true`。探测现在把该情况标为 `login_probe=unreadable`，不再将受限环境的无权读取当作用户未登录的证据。

本次验证：311 项测试运行完成，310 通过、1 跳过；前端 JavaScript 语法检查通过。SDK 环境探测仍输出既有资源关闭警告，不影响本次测试结果。

网页 CodeBuddy 登录改走 `console/login_flow.py`：项目 venv 子进程通过管道把白名单中国站链接交给服务端内存，前端轮询状态，尝试打开新标签并提供手动链接。认证失败、超时和启动异常均进入 failed，终态不保留链接；重复点击复用正在进行的认证。旧 CLI 手动登录保留。真实接口已观察 starting → waiting，人工完成登录回调仍待验收，不能据此声称账号登录成功。

更新时间：2026-09-11  
状态：`stage3: checkpoint supervisor entry`

## 本切片完成内容

- Slice A：新增 `orchestrator/core/supervisor_plan.py`，对主管返回结果执行严格 JSON 契约校验。
  - 只接受 `plan_version=1`、非空 summary 和非空 tasks。
  - 拒绝未知字段、重复/已存在 task id、未知 role/backend、非法 access mode、敏感文本、越界或冲突 write scope，以及未知依赖和环。
  - 计划只被规范化和计算摘要哈希，不产生外部副作用。
- Slice B：新增 `SQLiteStateStore.materialize_supervisor_plan`。
  - 在现有 controller/authority fencing 下，把计划原子写入任务和依赖图。
  - `plan.materialized` 事件携带计划摘要和子任务 ID；相同计划重复回调零副作用。
  - 非法结果通过 `plan.rejected` 事件记录，零子任务。
- Slice C：新增 `materialize_ready_supervisor_plans` 并接入 `serve`。
  - 仅处理成功且已提交的 supervisor backend call。
  - 计划解析失败、敏感内容、角色/后端/路径/依赖错误均拒绝。
  - 旧 controller epoch、旧 authority epoch、取消中的 supervisor 不会产生子任务。
- 网页入口：
  - `POST /api/runs/{run_id}/tasks` 支持 `mode=supervisor`，服务端强制 `read_only + supervisor`。
  - 控制台任务列表显示角色/后端和主管计划状态。
- Windows 回归修复：Git 子进程明确按 UTF-8 解码，恢复中文路径 worktree 校验。
- Slice D：模型选择与后端路由。
  - `task_dispatch_specs` 新增可选 `required_model`，schema 从 v13 迁移到 v14。
  - 控制台团队池和任务表单改为模型下拉框；Codex 通过当前登录态的只读模型目录动态加载，WorkBuddy/CodeBuddy 读取本地产品模型清单及 ArkCLI 用户级模型配置。
  - 调度器严格匹配 `required_backend + required_model`，不再把任务降级派给同后端的其他模型。
  - Codex `thread_start(model=...)` 与 CodeBuddy `CodeBuddyAgentOptions(model=...)` 均接入任务选择。
- Slice E：自定义提供方与火山 CodingPlan。
  - `agent_instances` 与 `task_dispatch_specs` 新增可选 `provider_id` / `required_provider_id`，schema 从 v14 迁移到 v15。
  - 同一 CodeBuddy/WorkBuddy 后端下可区分内置模型与自定义 CodingPlan 模型；调度器严格匹配提供方，不会把 CodingPlan 任务派到内置池。
  - 控制台支持维护自定义提供方：保存提供方 ID、标签、实际 Base URL、模型 ID 列表、默认模型和 API key 环境变量名；不接受也不保存实际 API key。
  - 执行时才从进程环境读取配置的 API key，并将 `CODEBUDDY_BASE_URL` 与 `CODEBUDDY_API_KEY` 注入子进程；缺失配置时以不可重试的 `provider_unavailable` 阻断。
- Slice F：WorkBuddy/CodeBuddy 模型连通性筛选。
  - 移除手动测试按钮；用户在任务或团队池选择 CodeBuddy 后自动逐个使用 `model` 参数发起无工具、单轮、不可持久化的最小连接测试，不执行用户任务文件操作。
  - 成功模型按内置模型与自定义提供方分别保存到 `.agent-hub/model-health.json`；后续目录接口和下拉框隐藏失败模型，模型清单发生变化时自动视为未验证，避免永久使用旧结果。
- 探测结果只返回成功/失败数量和模型 ID，不返回模型输出、异常原文或密钥；自定义提供方仍只从 API key 环境变量读取密钥。
- 控制台下拉框按来源分组显示内置模型与自定义提供方模型；选择火山等自定义提供方后，只显示该提供方的模型，并保留严格的 `provider_id` 路由。
- Slice G：ArkCLI 模型配置与 CodeBuddy 登录引导。
  - 读取 ArkCLI 支持的 WorkBuddy/CodeBuddy 用户级 `models.json`，只导入模型 ID/展示名，不复制 API key、URL 或其他敏感配置；用户级配置中的模型可直接进入模型下拉框。
- 一键登录脚本先切换到项目根目录再启动 CodeBuddy，避免从 `.bin` 工作目录触发错误的目录信任选择。
- 登录流程先切换到项目根目录并固定中国站环境；后台 SDK 认证不再依赖可见 CodeBuddy 登录窗口，用户级授权跨项目共享。

## 本轮补充：后台浏览器授权

- CodeBuddy 的一键授权改为后台启动 `python -m orchestrator auth codebuddy --open-browser`。
- SDK 认证流程自动打开一次性中国站授权页并等待回调，不再要求用户看到终端或输入 `/login`。
- 保留生成的 `.agent-hub/login/codebuddy-login.cmd` 作为手动回退脚本；正常网页流程不使用可见登录窗口。
- CLI `auth` 新增 `--open-browser` 选项；Codex 原有登录流程保持不变。

## 证据

## 本轮补充：团队编辑器状态与 Run 团队绑定

- 团队编辑器在新增或删除 Agent 池前先把当前 DOM 表单同步回内存模型，已填写的角色、池标识、数量、提供方和模型不会被重置。
- CodeBuddy 模型连通性探测按内置/自定义提供方缓存；编辑器重绘只复用缓存，不会因为新增或删除一行重复查询。
- 启动 Run 的 serve 不再弹出默认团队文件输入框。服务端根据 Run 保存的 team_id 解析已保存团队或默认团队文件；旧 Run 的 default 别名仍兼容回退。启动响应返回实际 team_id 与 team_path，便于排查绑定问题。

- 专项测试：主管计划、模型目录、提供方配置、提供方路由和真实适配器环境注入回归均通过；覆盖精确匹配、模型不降级、任务 API、团队模型下拉、CodingPlan 配置和密钥不落盘。
- 全量测试：**311 项，310 通过、1 跳过**（1 项按既有环境条件跳过）。
- 控制台专项和 JavaScript 语法检查通过。
- 数据库 schema 已为 v15，v14→v15 迁移通过。
- 登录引导与响应式兼容回归通过：Codex 保持桌面 `ShellExecute` 登录；CodeBuddy 改为后台 SDK 自动打开中国站浏览器授权页，并覆盖窄屏导航、卡片、表单、弹窗、长文本和 reduced-motion 样式。启动器与后台授权均优先复用项目 `.venv`，避免系统未配置 Python PATH 时后台 SDK 缺失；登录运行时回归已纳入测试。

## 未完成与下一切片

- 尚未完成真实 Codex supervisor 返回结构化计划的场景证据。
- 尚未提供“计划生成后、worker 派发前”的人工批准按钮；当前是 supervisor 成功后自动物化。该限制必须在后续 UI/审批切片中明确处理。
- 尚未把本切片单独跑通“主管自动生成的两个 worker → REVIEW → 人工批准 → Git 集成 → COMPLETED”的完整 Fake 验收。
- 尚未完成计划状态的更细粒度投影、父子任务查询和计划返工策略。

## 本切片补充：模型选择语义

- 团队池中的 `model` 是该池 Agent 的默认模型；不填写时保持既有行为，由后端池决定实际模型。
- 发起任务可以填写 `required_backend` 和 `required_model`；填写后必须匹配同一后端、同一模型的空闲 Agent，否则任务留在 READY，不会静默换模型。
- 主管自动物化的 Worker 计划目前仍沿用各后端池默认模型；主管任务本身可以在控制台选择模型。
- 人工批准按钮仍是后续切片；当前主管计划成功后仍按默认自动派发，产品设计保留 `auto` 默认并新增可选 `manual` 模式，不改变现有默认行为。

## 本轮补充：控制台审核、任务删除与 Agent 对话

- 审核不再因为 serve 长期持有 Run controller 而返回 `409 run controller is held by another owner`。仅人工审核/删除允许复用 serve 的当前 controller + authority fencing token；暂停、恢复、取消仍保持忙碌保护，避免控制台抢占调度器。
- 任务详情表现在显示任务说明和尝试次数；任务操作拆为“对话”和“删除”。删除限制在非运行状态，清理任务的外键子记录，保留 `task.deleted` 事件审计。
- 新增 `GET /api/runs/{run_id}/chat`，从持久化任务、协议消息和 backend call 结果构建按任务隔离的只读上下文；控制台新增“Agent 对话”页，支持 Run/任务选择、Agent/backend 标识和定时刷新。
- 修复 Run 详情在列表自动刷新时被重置的问题：保持当前展开 Run，不再因 8 秒轮询自动收起详情。
- 控制台重启后，Run/serve 状态会结合持久化 `serve-*` controller lease 展示，避免实际调度仍在运行时显示为“未启动”；显式点击停止后以本次控制台操作为准。

专项验证：`tests.test_stage3_console` 42 项通过；全量 `unittest discover` 311 项，310 通过、1 跳过；前端 JavaScript 语法检查通过。test003 真实复核与登录/超时回归沿用上次结果；新增 Run 团队绑定回归见下方。

因此本报告只支持 `stage3: checkpoint supervisor entry`，不支持新的 `stage3: complete` 结论。
本轮补充验证：tests.test_stage3_console 42 项通过；全量 unittest discover 311 项，310 通过、1 跳过；新增 Run 团队绑定回归确认省略 team_path 时使用 Run 对应的已保存团队，并自动迁移旧 default 别名。

## 本轮补充：Run 列表与真实主管计划诊断（2026-09-12）

- Run API 改为按创建时间倒序返回，新建 Run 位于列表顶部；前端详情展开移除 `scrollIntoView`，避免点击详情改变用户当前浏览位置；全局顶栏继续使用 sticky，并改用 `overflow-x: clip` 避免长列表滚动时被隐式滚动容器吞掉。
- 通过控制台删除明确命名的旧测试 Run `test001`、`test002`、`test003`、`test004`，仅清理受管 worktree/运行目录并保留 `run.deleted` 审计记录；`test005` 未删除。
- `test005` 的两次 Codex supervisor call 均返回了 `tasks` 但缺少顶层 `summary`，第一次还把 `supervisor_summary` 错误地放进 Worker 任务列表；严格校验因此产生两次 `plan.rejected`，随后 `plan.needs_input`，没有任何 Worker 被物化。主管提示模板已补齐顶层 `summary` 和全部必需任务字段，并明确由 Hub 在 Worker 完成后创建自然语言汇总任务。
- 专项回归与前端语法检查通过；全量 `unittest discover`：357 项，356 通过、1 跳过。
