# 个人开发版多 Agent 协作整改计划

编制日期：2026-09-11。状态：R0、R1、R2、R3、R4、R5 已验收；R6 对话工作台切片已完成并保持 checkpoint；R7–R8 待实施。

目标：用户向主管提交一次任务，由主管规划、两个 Worker 按需并行、独立 Reviewer 根据真实证据审核，系统完成有限返工、集成与交付；用户可以查看每个 Agent 的对话并给出引导。

本计划依据本地代码、PROJECT_PROGRESS.md、STAGE3_SUPERVISOR_ENTRY_REPORT.md，以及《深入理解 AI Agent》中文版第十章的上下文隔离、管理者模式、四类文件区域、移交包及独立验证原则编制。书籍提供设计参考，不作为本项目功能已实现的证据。

## 1. 与现有计划、报告的关系

- 原《跨Harness多Agent团队编排系统实施计划.md》继续约束阶段纪律、凭据边界、SQLite 权威状态和 Git 交付。本文件是个人版整改执行补充，不撤销或重写历史签字。
- 本轮沿用 `stage3: checkpoint ...`，使用 R0–R8 标识整改切片；完成本文件全部出口后才能声明“个人开发版验收完成”。历史 Beta PASS 不代表本版本已通过。
- `PROJECT_PROGRESS.md` 是状态入口；本文件定义执行步骤；新建的 `docs/PERSONAL_EDITION_REMEDIATION_REPORT.md` 应记录每个切片的实际证据。报告在 R0 创建。
- `docs/CHAT_WORKSPACE_PLAN.md` 的 H1–H5 收入 R6；会话与引导能力以安装版本实测为准，旧文档的 SDK 能力描述不能替代验证。
- 原报告同时出现自动物化和计划前审批的描述。本版本冻结为：新 Run 默认人工批准计划；用户可明确选择预授权自动模式。已有 Run 保留原策略，禁止迁移时静默扩权。
- 本次只编制计划和维护入口，不实施运行时代码、不改变活动 Run、不重放历史任务。

## 2. 首版产品范围与固定原则

| 项目 | 本版本决定 |
|---|---|
| 主管 | 默认 1 个 Codex Supervisor，后端/模型可配置，负责规划、重规划与汇总 |
| Worker | 默认 2 个，可设 1–4 个；只对独立任务并行，不强制所有任务拆分 |
| Reviewer | 独立角色与会话，按审核阶段启动；可复用闲置计算槽，但不得复用作者任务上下文 |
| 并发 | 默认最多 3 个活动后端调用；Reviewer 计入上限，不能绕过配额 |
| 上下文 | 项目规则共享，任务轨迹隔离；同任务恢复可续会话，新任务默认新上下文 |
| 写入 | 每个写任务尝试独立 worktree；代码候选提交后不可原地修改 |
| 完成判定 | 后端成功、产物有效、审核通过、集成成功分别记录；父任务只有全部必要子任务交付后完成 |
| 返工 | 首次候选后最多 2 轮返工；格式修复最多 1 次，另计但同样消耗总调用/时间预算 |
| 自动化 | 计划获批后自动执行、取证、审核、有限返工；最终合并按 Run 明确保存的人工/自动策略执行 |
| 外部资源 | 保留挂载描述接口；首版无需接入云盘或知识库，未配置显示未启用 |
| 模型账号 | 复用已验证的登录/提供方适配器；不要求用户为了整改购买 API |

“3 Agent”有两个明确验收场景：主管 + 两个 Worker 的真实派发；规划、执行、独立审核的三个逻辑角色。Reviewer 按需运行，因此团队存在四个角色实例不意味着四路同时调用。

暂缓：去中心化 Swarm、无限递归派生、Agent 经济、全量轨迹广播、向量记忆平台、跨组织 A2A、通用云盘虚拟文件系统、为所有任务强制多 Agent 辩论。

## 3. 起点核验与已知缺口

最近记录的测试是 311 项运行、310 通过、1 跳过；这是历史基线，实施时重新运行。schema 最近记录为 v15，迁移编号须读取实际数据库后确定。

| 已检查位置 | 当前证据与整改含义 |
|---|---|
| `orchestrator/core/config.py` | RoleSpec 只有标识、版本、标题、能力；缺职责与输入输出协议 |
| `orchestrator/core/supervisor_plan.py` | 有严格计划校验，可复用；缺验收项、产物引用、完整预算契约 |
| `orchestrator/core/supervisor_planning.py` | 普通文本会拒绝；处理标记含 plan.rejected，需设计版本化修复，避免拒绝一次后永久跳过 |
| `orchestrator/adapters/real.py` | 两个适配器声明 structured output=false；不能直接假定安装的 SDK 已支持 Schema |
| `orchestrator/console/server.py` | ensure_run_worktree 按 Run 建副本，需要改为任务尝试级隔离及单独集成区 |
| `orchestrator/workspace/git_manager.py` | commit_managed_changes 接受额外辅助文件；创建时校验 write_scope 并不能约束 Agent 实际写入，须补实际 diff/越界检测 |
| `orchestrator/storage/sqlite_store.py` | 已有消息、调用、租约、审批、预算、outbox；须复用并核验，不另建竞争状态源 |
| `orchestrator/console/static/index.html` | 对话以任务结果回放为主；持久 Agent 会话导航、引导投递尚待实现 |
| `PROJECT_PROGRESS.md` | 含历史未勾选项与后续完成记录，应分开“历史审计”“当前未完成”，避免读者误判 |

## 4. 模块边界与事实源

保留：现有 Adapter、调度器、fencing、SQLite、Task DAG、Git 集成、审批和回归测试。新增模块应小而独立，文件名可随实际实现调整，但职责与验收不变。

| 模块 | 责任 | 禁止承担 |
|---|---|---|
| Role registry / prompt builder | 解析版本化角色，组装职责和任务输入 | 用提示词替代文件/工具权限 |
| Plan service | 保存、验证、批准和修订计划 | 直接执行任意模型生成代码 |
| Context builder | 按任务生成移交包、选择事实和引用 | 默认注入整个项目或其他 Agent 的轨迹 |
| Workspace manager | 私有区、受管副本、候选与证据生命周期 | 让 Worker 写用户 checkout 或其他任务副本 |
| Runtime / scheduler | 派发、预算预留、消息投递、取消、恢复 | 接受模型自行改写批准或完成状态 |
| Verification service | 针对候选 commit 运行检查并保存证据 | 把 Worker 自报测试通过当作独立证据 |
| Console | 展示状态与提交用户意图 | 复制服务端租约 token 后自行充当 serve |

SQLite 是计划状态、任务状态、消息投递和审批的唯一事实源。Markdown 进度是只读投影；产物文件是不可变内容，由数据库记录路径、摘要、版本、归属和状态。UI 日志可供用户查看，但不自动进入其他 Agent 的模型上下文。

## 5. 文件布局与访问规则

建议布局如下。只有 R0 备份、R2 实现后才创建或迁移运行目录，不对旧 Run 批量移动。

```text
config/
  roles/                         # 可版本管理的角色源模板
  schemas/                       # 移交、计划、结果、审核 Schema
.agent-hub/
  state/                         # SQLite 与备份，仅 Hub 可写
  resources/<version>/           # 固定版本的角色、技能、模板，只读
  runs/<run-id>/
    scratch/<agent-id>/<attempt-id>/
    shared/
      inputs/                    # 用户输入快照，Agent 只读
      handoffs/                  # Hub 生成的任务包，接收者只读
      artifacts/<task>/<attempt>/ # 已发布候选，发布后只读
      evidence/<task>/<attempt>/  # 独立验证器写，Worker 只读
      progress/                  # Hub 从数据库生成的轻量视图
    mounts/manifest.json         # 外部授权资源描述，默认空
    worktrees/<task>-<attempt>/   # 写任务专用代码副本
    integration/                # Hub 的串行合并/验证工作区
```

四类逻辑区域：scratch 是私有区；shared 与受管代码副本属于协作区；mounts 是外部资源；resources 是系统只读资源。state、租约与凭据属于运行时管理空间，不开放给 Agent。

具体约束：

1. 路径由 Hub 分配；模型只提供逻辑引用，不能指定任意本机绝对路径。
2. 每个尝试从明确 base commit 创建 worktree；依赖任务以已验证集成版本作为下游起点，禁止无意识使用过期基础版本。
3. Scratchpad 是逻辑隔离，真正的硬隔离需后端 sandbox/工具拦截/OS 权限支持。相同 Windows 用户下仅创建不同目录不构成安全隔离。
4. R0/R2 实测两个后端的实际读写边界；不能可靠限制的后端不得用于无人值守写任务，保留只读用途并记录可用范围。
5. worktree 隔离代码文件，但 Git 对象库和部分元数据仍共享；禁止 Worker 任意清理分支、删除 worktree、推送或操作其他任务进程。Git 发布由 Hub 统一完成。
6. 实际变更超出 write_scope 时隔离该候选并报错；不接受“未提交的额外文件可忽略”。Scratch 中的辅助文件不属于代码提交范围。
7. 并行任务不仅比较文件路径，还声明接口/数据结构等共享资源的所有权。不能安全拆分的工作按依赖串行执行。
8. 审核证据绑定 candidate commit、检查定义摘要和执行环境；候选变化、基础版本变化后旧批准失效并重新验证。
9. 默认保留失败尝试供排查；清理只处理已结束且无引用的私有临时数据，展示预览，不删正式产物、用户目录或活动副本。

## 6. 三份核心契约

### 6.1 角色模板

至少包含 role_id、version、title、goal、responsibilities、forbidden_actions、input_contract、output_contract、tool_policy、context_policy、budget_defaults。配置模型/提供方在 Agent 池中，避免角色与厂商绑定。

模板分 supervisor、implementation-worker、reviewer；研究能力先作为 Worker 的任务类型。团队配置引用模板和可选覆盖，保存时验证最终配置，并在 Run 启动时冻结快照。

指令组装顺序：基础项目规则 → 角色规程 → 团队覆盖 → 已批准任务包 → 有来源标签的用户引导。后续文本不能扩大硬权限；用户要求扩大范围时，转为计划修订与重新批准。

### 6.2 任务移交包

必需字段：contract_version、project_id、run_id、task_id、attempt_id、plan_revision、goal、accepted_facts、constraints、input_refs、base_commit、write_scope、acceptance_criteria、budget、expected_output。事实与引用包含来源和版本；不把“模型猜测”登记为已验证事实。

每个任务必须说明做什么、哪些不在范围、读取什么、交付什么、如何证明完成。大文件通过引用按需读取；包内只包含必要摘要。

### 6.3 结果与审核

WorkerResult：状态、简短总结、candidate_commit、artifact_refs、claimed_checks、remaining_issues、需要输入的问题。claimed_checks 只是自报，不直接赋予通过状态。

ReviewResult：pass/rework/blocked、逐条验收结论、evidence_refs、可定位的问题及修复条件。引用缺失或证据与 commit 不匹配时不能 pass。Reviewer 只写审核报告，不能改候选代码或降低检查门槛。

## 7. 逐切片实施步骤

所有切片开始前读取 AGENTS.md 和相关报告。每次行为变化补充有意义的失败回归并运行项目全量单元测试；记录命令、结果、跳过原因和变更 commit。Fake 验证与真实后端验证分别记录。

### R0：冻结基线与能力实测

依赖：无。目标：保证整改起点可恢复、能力描述有证据。

1. 记录 git HEAD、分支、dirty 清单、当前 schema、活动 Run；保护现有未提交修改，禁止批量 reset/clean。
2. 使用现有 db_ops 在线备份数据库；在临时数据库演练恢复与完整性检查，不覆盖活动库。
3. 在项目根目录运行 `.\.venv\Scripts\python.exe -m unittest discover -s tests`，保存基线；若路径不存在，先从启动脚本确定解释器。
4. 对已安装 Codex/CodeBuddy SDK 建能力表：结构化输出、session 恢复、流式文本/工具事件、活动引导、取消确认、读写限制、usage。记录版本和最小探测结果，不输出凭据。
5. 对当前模型成功/业务失败分层归因，保存 Run 6 的 JSON 拒绝为回归样本；不自动重试旧 Run。
6. 新建整改报告，统一当前待办入口，将旧报告的历史数字保留为历史。

出口：备份可恢复、基线可重现、能力表无“推定支持”、每个缺口有明确后续切片。早期能力闸门失败写入 SPIKE_REPORT.md，依赖该能力的切片停止。

提交建议：`stage3: checkpoint freeze personal edition baseline`。

### R1：角色模板、计划协议与批准状态

依赖：R0。主要位置：core/config.py、core/supervisor_plan.py、core/supervisor_planning.py、adapters/contracts.py、adapters/real.py、sqlite_store.py。

1. 增加版本化角色加载和 prompt builder，提供三份默认模板。旧 team schema 自动解析为兼容模板，缺角色/模型时显示阻塞原因。
2. Run 保存团队和角色的不可变快照与摘要；编辑团队只影响新 Run，恢复旧 Run 使用原快照。
3. 升级计划契约：增加验收项、输入引用、任务种类、输出契约；任务数量上限与 Worker 并发数分离，允许两个 Worker 顺序完成多于两个任务。
4. 向主管提供当前角色/池能力目录、任务预算和合法计划示例；后端支持时使用结构化输出，不支持时使用明确 JSON 协议加本地校验。
5. 增加 plan revision 和状态：生成中 → 待批准 → 已批准 → 执行中 → 验证中 → 已交付；另有格式失败、需修订、取消。具体映射复用现有状态机，不破坏历史 task 状态。
6. 格式失败最多修复一次，输入为原任务、失败输出及结构化错误；生成新的调用记录，不擦除原失败；仍失败则需要输入，零 Worker 派发。
7. 计划批准绑定规范化摘要、revision 和权限范围，并原子消费；审批过期/计划改变/重复点击不得重复派发。
8. UI 提供简版计划预览和批准/退回；新 Run 默认 manual，显式 auto 的策略在创建时保存。
9. 将父任务完成与“主管模型返回成功”分开；只生成计划不能被统计为整个任务完成。

测试：普通文本自动修复、非法路径/循环拒绝、角色/模型不匹配、两个槽六个任务、审批前零派发、重复批准幂等、旧 rejected 事件不阻止新 revision、父任务不假完成。

出口：Fake 主管计划可预览批准；真实 Codex 可生成合法双 Worker 计划或在失败时明确阻塞，不再显示含糊的成功。

提交建议：`stage3: checkpoint role contracts and approved plans`。

### R2：任务尝试隔离与四区存储

依赖：R1。主要位置：workspace/policy.py、workspace/git_manager.py、console/server.py、scheduler.py、sqlite_store.py。

1. 实现 Run workspace manifest，登记四区路径、owner、权限、生命周期和资源版本。
2. 将写任务 cwd 从 Run worktree 改为 task/attempt worktree；主管和 Reviewer 读取明确快照，不能写 Worker 副本。
3. 新增 scratch 分配与只读资源快照；把辅助日志/草稿从代码工作区分离。
4. 增加 artifact manifest：task、attempt、路径、内容摘要、candidate commit、生产者、发布时间；发布后修改必须生成新版本。
5. 依据 R0 能力表实施后端实际权限；测试相邻 scratch、Hub 数据库、项目外路径、symlink/junction 与 `.git` 操作，记录硬限制和仅事后检测的区别。
6. 提交前比较实际 diff 与 write_scope；禁止意外 stage 其他 Agent 文件，新增/删除/重命名均检查。
7. 集成区由 Hub 串行管理，用户 checkout 不参与自动并行修改。提供集成 commit 和交付 diff，应用到用户分支遵循已保存的策略。
8. 旧 Run 继续旧目录模式并标记 legacy；新 Run 才启用新布局。只提供旧 Run 结束后的显式迁移，不移动活动副本。

测试：双写同名文件副本隔离、跨区访问、scope 外变更、失败后保留证据、依赖基线更新、用户 checkout 指纹不变、重启重新识别副本。

出口：两个 Worker 真正并行写入独立受管副本，产物归属清楚；无法可靠约束的后端明确禁止无人值守写模式。

提交建议：`stage3: checkpoint isolated task workspaces`。

### R3：移交、执行、结果回收与父任务聚合

依赖：R2。主要位置：scheduler.py、call_runtime.py、agent_pool.py、serve.py、core/models.py、sqlite_store.py；新增 context builder。

1. 由已批准计划生成移交包，记录摘要和输入版本；工作目录、权限与预算取自 Hub，不信任模型给出的替代路径。
2. 将 Agent 身份、role binding、provider session、task attempt 分开；新任务新上下文，同任务恢复沿用原会话或明确标记替代会话。
3. Worker 返回结构化结果并发布产物，校验 candidate 存在、归属正确、引用可解析；纯文本声称其他 Worker 已完成不能创建完成证据。
4. 上游必要任务验证/集成后才释放依赖；下游获得明确的更新后 base commit 和产物引用。
5. Manager 汇总只读取状态、已验证摘要和引用；失败/需输入保留原始证据并触发受限重规划。
6. 定义父任务聚合：任一必需子任务未交付则父任务不完成；可选任务失败要显式披露；取消父任务级联取消子任务。
7. 在 API 中展示父子关系、派发来源、实际 Agent/session/backend/model，不用 UI 名称推断执行者。

测试：任务上下文隔离、依赖数据准确传递、重启恢复不重复创建会话/任务、伪造结果拒绝、父任务聚合、晚到旧结果隔离。

出口：只向主管发送“让两个 Worker 分别返回 1 和 2”，观察到两个不同 Worker 的真实调用及各自结果；主管的最终汇总引用这些结果。

提交建议：`stage3: checkpoint explicit handoffs and result aggregation`。

### R4：独立验证、返工与 Git 交付

依赖：R3。主要位置：workspace/merge_executor.py、sqlite_store.py、serve.py；新增 verification service。

1. 从任务验收标准选择预先配置的检查；Agent 推荐的任意 shell 命令不能未经策略验证直接成为验证器命令。
2. 在隔离验证副本对固定 candidate 执行测试/构建/运行；前端任务保存真实页面截图与交互结果。记录 commit、命令、退出码、输出摘要和环境。
3. Reviewer 使用原始目标、候选 diff 和独立证据，不继承作者轨迹。要求逐条验收，证据不足返回 blocked。
4. 审核失败创建新的 attempt，携带可定位修复条件；保留旧候选与证据。最多两轮返工后需要用户处理。
5. 仅审核通过的候选允许进 merge queue；计划批准与最终合并批准分开，批准均绑定对应 revision/commit。
6. 集成串行进行；合并后的组合代码重新运行必要测试，发现语义冲突则集成失败/待修复，不置 COMPLETED。
7. 只有有效交付结果、审核和集成对账齐全时原子写完成事件及 outbox；崩溃重启重对账，不能重复合并或通知。
8. 最终报告列明完成项、交付 commit、验证证据、未完成项及后续人工动作。

测试：Worker 假报通过、Reviewer 无证据、候选改动后旧审批失效、返工上限、测试基线被篡改、跨文件接口冲突、合并崩溃恢复。

出口：真实小型代码任务完成规划 → 并行实现 → 独立检查 → 至少一次可控返工 → 集成 → 父任务完成。Fake 全流程通过不能替代该实测。

提交建议：`stage3: checkpoint evidence based review and delivery`。

### R5：消息投递、取消与预算

依赖：R4。基础调用/时间/并发限制从 R1 起就必须生效，本切片补全消息与回收语义。

1. 扩展现有 MessageEnvelope，保留 idempotency_key/correlation_id；加入目标 Agent、attempt、plan revision、来源、类型和有效期。
2. 定义 dispatch/status_update/result/review/user_guidance/terminate/ack；SQLite 记录 queued/delivered/acknowledged/failed/expired。
3. 单一 serve 在 fencing 下领取控制命令，API 只提交意图；替换控制台复用 serve token 直接操作的捷径，避免形成多写者。
4. 采用至少一次投递 + 消费方幂等；关键状态变更与事件/outbox 同事务。不声称网络 exactly-once。
5. 取消先发信号并等待确认，超时标记 backend_may_still_run；后端未确认停止前不能立即把同一受控资源交给下一次写任务。
6. 并发槽、最大调用数、总运行时长、任务上限在派发前原子预留；失败重试和格式修复同样计费计次。
7. 仅后端提供可靠 usage 时启用 Token 统计；订阅额度/金额未知显示未知，不按零消耗计算。使用时间和调用硬上限兜底。
8. 增加进度心跳与无活动告警；慢响应先检查状态，不能仅凭 progress 文件未更新就认定进程死亡。

测试：重复消息、重启未 ack、旧 attempt 引导、取消不确认、并发抢占预算、预算耗尽、晚到事件、控制台/serve 竞争。

出口：消息不静默丢失，取消与预算状态可解释，运行重启不会多派发或超额启动。

提交建议：`stage3: checkpoint durable messaging and bounded execution`。

### R6：项目—角色—Agent 对话工作台

依赖：R5。主要位置：console/server.py、console/static/index.html、console/settings.py、serve.py、adapters/real.py；承接旧 H1–H5。

1. 增加 Project：名称、本地 workspace、默认团队；先采用用户输入绝对路径加服务端验证，原生目录选择只在环境支持时增强。
2. 左栏显示项目 → 角色 → Agent；Run 为执行记录筛选，Task 为 Agent 内的上下文列表，不将所有历史混为同一模型会话。
3. 对话显示用户、主管派发、Agent 可见输出、工具事件摘要、系统状态；活动反馈只展示后端实际提供的数据，不补造流式轨迹。
4. 采用按 cursor/sequence 增量刷新，保留选中项、滚动位置、输入草稿及团队编辑状态；新输出到达时提供未读标记。
5. 支持空闲 Agent 发起任务与运行中引导；消息先持久化，显示排队、送达、应用、失败。由主管接收的新任务自动进入计划流程。
6. 基于 R0 结果接入同会话续接；活动插话仅在后端已验证支持时显示。其他后端使用本轮结束后投递，无法续接时明确标记新会话并带移交包。
7. 引导改变任务范围时暂停并请求计划修订；自然语言不能扩大 write_scope、模型提供方或批准范围。
8. 审核抽屉提供验收清单、diff、证据和退回原因；“通过”按钮仅在有候选与证据时可用，错误提示显示可执行下一步。
9. 团队编辑器提供内置角色模板选择、预览及覆盖，修改一行不丢其他输入；Run 页面显示生效配置快照。

测试：从浏览器创建项目/团队/Run、刷新保持状态、两 Worker 不串上下文、引导无重复/失效有提示、详情不折叠、审核材料可见、窄屏可操作。

出口：用户仅操作控制台即可创建并完成任务，能观察派发和各 Agent 输出、给出引导、完成审核，不需要编辑 JSON 或手工逐个分派。

提交建议：`stage3: checkpoint project agent conversation workspace`。

### R7：恢复、迁移与个人版发布准备

依赖：R6。

1. 以实际 schema 版本顺序迁移，迁移前备份；测试旧 team/Run/任务的读取和新 Run 创建，禁止篡改历史结果。
2. 针对计划批准前后、任务派发后、候选发布后、审核后、合并前后、引导入队后注入进程中断并恢复。
3. 检查临时进程、worktree、锁和并发预留回收；不把仅 UI 停止当作后端已停止。
4. 提供运行历史归档与清理预览；删除活动任务继续受限，删除历史任务保留必要审计和证据引用完整性。
5. 更新启动说明、端口选择、故障自查、数据库恢复说明；继续避开用户 Steam 的 8080，不终止非本项目进程。
6. 梳理 PROJECT_PROGRESS 当前/历史状态，报告必须区分真实/Fake、通过/失败/跳过/未测。

出口：升级和恢复可重复，旧数据可读，用户能按文档自行启动与排错。

提交建议：`stage3: checkpoint recovery and personal edition documentation`。

### R8：真实端到端验收与投入使用

依赖：R7。固定同一小型样例项目、输入和验收标准，保存运行记录。

| 场景 | 必须观察到的结果 |
|---|---|
| A：主管 + 双 Worker 返回 1/2 | 两个真实 Worker 调用、可区分 Agent/session、主管引用真实结果 |
| B：并行修改两个独立模块 | 调用时间存在重叠，各自副本/commit，用户 checkout 未被改写 |
| C：依赖任务 | 下游读取上游已验证版本，不能读初始旧文件 |
| D：故意放入缺陷 | 独立验证发现，Reviewer 给定位条件，返工修复后重新验证 |
| E：格式错误与非法计划 | 格式最多修复一次；越界计划零副作用；没有假完成 |
| F：失败恢复与取消 | 重启不重复派发/合并；不支持确认取消时明确显示不确定 |
| G：运行中用户引导 | 可查目标、投递状态和实际生效时机，不串任务、不扩权 |
| H：预算上限 | 达到调用/时间/并发上限停止新增工作，说明剩余未完成项 |
| I：审核和集成 | 用户看得到证据；旧证据不能批准新 commit；组合代码检查通过 |
| J：单 Agent 对照 | 同目标记录成功率、墙钟时间、实际可得 usage 和人工介入次数 |

A、B、D 至少各重复 3 次；每次记录失败，不只挑成功截图。对照先以相同任务、模型条件和调用/时间预算运行；若 Token 数据不可得，披露不能声称等 Token 优势。小样本仅支持本地可用性，不作普遍性能结论。

任一核心真实场景失败保留 checkpoint。最终签字至少满足：一次输入可完整交付、真实并行、独立验证、有限返工、重启不重复副作用、引导状态准确、预算可控、用户能复现。

提交建议：验收前 `stage3: checkpoint personal edition acceptance`；全部本版本出口满足后使用 `stage3: complete personal edition verified workflow`，提交说明列明本版本范围与外部挂载等未启用功能。

## 8. 执行台账

| 切片 | 状态 | 完成 commit | 测试/真实证据 | 阻塞 |
|---|---|---|---|---|
| R0 基线与能力 | **完成** | 历史基线提交 | `docs/PERSONAL_EDITION_REMEDIATION_REPORT.md`；备份恢复通过；311/310/1；能力表与 Run 6 样本 | — |
| R1 角色与计划 | **完成** | `0849562` | Fake 计划/批准/幂等通过；真实 Codex 合法计划未验证，见整改报告 | 依赖 R0 |
| R2 四区与隔离 | **完成** | `8c9f710` | 四区/副本/scope 对账通过；真实后端权限未重测，见整改报告 | 依赖 R1 |
| R3 移交与聚合 | **完成** | 本轮 R3 提交（见 `git log`） | handoff/result/父任务聚合 Fake 证据通过；全量 321/320/1；真实后端汇总未验证 | 依赖 R2 |
| R4 验证与交付 | **完成** | 本轮 R4 提交（见 `git log`） | 固定检查/evidence-bound 审核/旧证据拒绝/两轮返工专项通过；真实后端未验证 | 依赖 R3 |
| R5 消息与预算 | **完成** | 本轮 R5 提交（见 `git log`） | schema v20；durable deliveries；取消确认与资源保留；进度心跳和预算 reservation；全量 331/330/1；真实后端即时引导/取消确认未验证 | 依赖 R4 |
| R6 对话工作台 | **checkpoint（切片完成）** | 本轮 `stage3: checkpoint project agent conversation workspace` | 项目元数据 API；项目→角色→Agent 导航；chat cursor/events 与 Agent 过滤；运行中引导 durable 入队；全量 335/334/1 | 真实活动插话、跨 workspace 热切换和完整浏览器端到端仍待 R7/R8 处理 |
| R7 恢复与文档 | 待开始 | — | — | 依赖 R6 |
| R8 真实验收 | 待开始 | — | — | 依赖 R7 |

建议每个切片内按“契约/测试 → 最小实现 → 回归 → 真实证据（适用时）→ 文档 → commit”完成。一个切片过大时追加 Rn-a/Rn-b，不跳过出口。按用户既有要求在已完成切片后提交并推送当前项目仓库；提交前核查实际 diff，仅包含该切片相关修改，禁止把凭据、运行数据、下载缓存或其他未归属改动一起上传。

## 9. 下一位 Agent 的起步说明

1. 阅读 AGENTS.md、PROJECT_PROGRESS.md、本计划和主管入口报告。
2. 检查 git 状态，确认工作区已有改动，不覆盖或自动提交未知改动。
3. 从 R0 开始，先给出当前基线、能力表与未通过项。
4. 按 R1–R8 实施，每个切片通过对应出口后再推进。
5. 遇到后端不支持的能力，记录限制并执行本计划的明确降级；不得把新 query 标成同会话引导、把本地目录标成安全沙箱、把后端成功标成业务完成。
6. 每次交接说明已完成切片、证据路径、提交、活动进程/Run、遗留问题和下一条具体步骤。

本计划完成标志是用户能在控制台独立完成一次真实开发任务；测试数量、界面功能数量和 Agent 人数均不能单独代替这一结果。
