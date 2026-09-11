# Cross-Harness Agent Team Orchestrator

这是一个本地多 Agent 编排器，目标是让 Codex 与 CodeBuddy/WorkBuddy 在同一项目中承担可配置的主管、执行和审核职位。

当前状态：**阶段 0 已 GO；阶段 1 已通过；阶段 2（MVP）已整改完成并重新签字 PASS；阶段 3 Beta 历史签字完成，R0–R5 已完成，R6 对话工作台、R7 恢复/文档已提交，R8 真实验收门禁已建立但仍为 checkpoint。** 审计发现的 3 项 P0 与 MVP 必需 P1 已全部修复（authority fencing、workspace 边界、merge/git/outbox 原子闭环、真实写任务、超时/脱敏、终态口径）；Beta 再补项 B1–B3（金额预算、审批原子消费、Outbox 重试）已在本阶段内完成。阶段 3 退出条件 E1–E7 PASS，E8（30 分钟干净 Windows 安装演示）经决策跳过（开发自用）。当前全量 **345 项，344 通过、1 跳过**；CodeBuddy 实时探针要求交互登录，R8 双后端真实端到端验收仍未完成。

当前状态入口：[PROJECT_PROGRESS.md](PROJECT_PROGRESS.md)。开发纪律见 [AGENTS.md](AGENTS.md)，安装与 30 分钟演示见 [docs/INSTALL.md](docs/INSTALL.md)，审计原文与 WorkBuddy 对账分别见 [STAGE2_AUDIT_FINDINGS.md](STAGE2_AUDIT_FINDINGS.md) 和 [STAGE2_AUDIT_RESPONSE.md](STAGE2_AUDIT_RESPONSE.md)。

## 当前交付范围

- 检测 Codex 与 CodeBuddy Python SDK 是否可用。
- 只检查本机是否存在 Codex 已保存登录，不读取或输出凭据。
- 提供统一 JSON 探针结果，供后续 Adapter 能力矩阵使用。
- 可选择运行一次只读在线问答，验证账号登录和 SDK 调用链。
- 验证 CodeBuddy 双 Session 并发、上下文/cwd 隔离和 Session 恢复。
- 验证根内受控写入与 Adapter 越界零调用拒绝。
- 验证 Codex Session 恢复、失败、超时、取消和临时任务归档。
- 提供 `1 Codex + 2 CodeBuddy` 的团队/职位配置。
- 提供 Task / Attempt 状态机、结构化消息和 SQLite 追加审计事件。
- 提供 Agent / RoleBinding / Session 生命周期、schema v4 Backend Call 持久化和 generation late-result fencing。
- 提供可取消、可轮询、单 Session 串行的统一 Fake Backend Adapter，以及 1/2/4 Agent Pool 扩缩容和安全 drain。
- 提供原子 Task 领取、并行 Fake 调度、Run Controller lease/epoch fencing、按单调用崩溃恢复，以及旧 controller 回调零副作用保证。
- 提供原子 Task DAG 创建、fan-in 依赖解除、失败/取消级联、优先级调度和带上限的指数退避。
- 提供最小初始化与状态查询命令。

## 本地运行

项目使用隔离虚拟环境。Windows PowerShell 示例：

```powershell
.\.venv\Scripts\python.exe -m orchestrator init
.\.venv\Scripts\python.exe -m orchestrator status
.\.venv\Scripts\python.exe -m orchestrator status --run <run-id>
.\.venv\Scripts\python.exe -m orchestrator reconcile --run <run-id>
.\.venv\Scripts\python.exe -m orchestrator recovery-check --run <run-id>  # 只读恢复检查
.\.venv\Scripts\python.exe -m orchestrator history-preview --older-than-days 30
.\.venv\Scripts\python.exe -m orchestrator console --run <run-id> --port 8081   # 8080 被 Steam 占用时使用 8081
.\.venv\Scripts\python.exe -m orchestrator serve-team --run <run-id> --team <team名>   # 按 team 常驻调度
.\.venv\Scripts\python.exe -m orchestrator demo --fake
.\.venv\Scripts\python.exe -m orchestrator demo --git-fake
.\.venv\Scripts\python.exe -m orchestrator demo --recovery-fake
.\.venv\Scripts\python.exe -m orchestrator demo --real
.\.venv\Scripts\python.exe -m orchestrator probe
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

## 自己上手（开发自用）

**一键启动：双击仓库根目录的 `start.cmd`**（或命令行 `.\start.cmd`）。脚本会自动：

> 不会用控制台？先读 5 分钟傻瓜教程：[docs/CONSOLE_GUIDE.md](docs/CONSOLE_GUIDE.md)。

1. 首次运行自动建 `.venv` + 装依赖（`scripts/bootstrap.py`）；
2. 设置 CodeBuddy 中国站所需环境变量；
3. 从 8080 起自动挑一个空闲端口拉起网页控制台，并打开对应地址（如 `http://127.0.0.1:8080`；被占则顺延 8081/8082…）。

> **如果浏览器打开后看到 "Steam / Inspectable Web Contents" 页面**：那是 Steam 占用了 8080（它的内嵌浏览器调试服务），不是本控制台。`start.cmd` 会自动绕开被占端口，控制台窗口标题与启动日志里会打印真实地址；手动执行 `orchestrator console` 时服务端也会自动顺延端口并打印实际地址。

常用变体：

```powershell
.\start.cmd                # 一键启动（等价上面三条）
.\start.cmd serve run-1 config\team.yaml   # 额外把该 run 的调度器窗口一起拉起
.\start.cmd check          # 只打印环境状态，不起服务
```

进入控制台后的日常路径（无需再碰命令行）：

- **Connections**：探测 codex/codebuddy 登录态，未登录按页面引导执行 `auth`；
- **Teams**：鼠标组建临时 team（backend/role/count），或直接用默认 `config/team.yaml`（1 codex 主管 + 2 codebuddy worker）；
- **Runs**：新建 Run → 一键启动/停止该 Run 的调度器（日志 `.agent-hub/logs/`）→ 发起任务 → 在任务详情里审批。

> 注意：跑 codebuddy 任务时需设两个环境变量（中国站），否则该后端任务会失败——
> `start.cmd` 已自动处理；若手动在终端跑 `console`/`serve-team`，请先执行：
>
> ```powershell
> $env:CODEBUDDY_SKIP_GIT_BASH_CHECK = "1"
> $env:CODEBUDDY_INTERNET_ENVIRONMENT = "internal"
> ```
>
> 浏览器登录态不会自动共享给独立 SDK；首次使用先执行 `orchestrator auth codex` 与 `orchestrator auth codebuddy`（见下）。

`init` 会校验 [团队配置](config/team.yaml)，并把运行状态初始化到 `.agent-hub/state/agent-hub.db`；已有数据库按编号自动迁移到当前 schema，不改写历史 Run。配置文件采用 JSON-compatible YAML 1.2，以便编排核心继续只依赖 Python 标准库。

`status --run` 提供隔离的单 Run 只读汇总；`recovery-check` 只读检查控制器/主管租约、活动任务、过期 Assignment Lease、未确认后端调用、消息投递和预算预留；`reconcile` 执行一次显式状态协调，只回收已过期且仍为 ACTIVE 的 Assignment Lease。恢复时会再次按 generation、状态和同一 cutoff 做条件更新，避免扫描后 Worker 已续租却被误回收。`history-preview` 只列出满足时间和终态条件的历史 Run，不执行删除。

`demo --fake` 不调用在线模型，用于验证两个 Worker 的并行、Reviewer 驳回和新 Attempt 返工。运行报告写入 `.agent-hub/reports/`。

`demo --git-fake` 在 `.agent-hub` 的隔离仓库中验证并行 worktree、串行集成、确定性测试和同路径冲突阻断；`demo --recovery-fake` 会启动并强制终止可控子进程，验证租约回收、重排、明确失败和旧代结果 fencing。两者都不会调用在线模型或修改当前 checkout。

`demo --real` 运行固定的真实验收场景：一个 Codex Plus Thread 负责规划和审核，两个中国站 CodeBuddy Session 并行产出，B 首次提交被驳回后由新 Attempt / Session 返工。单次成功显示 `run-passed`；连续三次成功后才显示 `ready`。每次运行都会保留报告和不可挑样的验收历史。

R8 个人版真实验收门禁：

```powershell
& '.venv\\Scripts\\python.exe' scripts\\personal_r8_acceptance.py
```

该命令只读取历史报告并输出 `CHECKPOINT`；只有把固定样例项目的真实 A–J 证据写成 `personal-r8-v1` 后，再用 `--evidence` 复核，才可能输出 `COMPLETE`。历史 20 场景 PASS 不能替代 R8 的主管汇总、独立审核/返工、重启/取消、用户引导和单 Agent 对照。

桌面端登录不会自动共享给独立 SDK。首次使用时分别执行：

```powershell
.\.venv\Scripts\python.exe -m orchestrator auth codex
.\.venv\Scripts\python.exe -m orchestrator auth codebuddy
```

CodeBuddy 已固定使用中国站（`copilot.tencent.com`，SDK 环境 `internal`），不会跳转到国际站。网页「一键登录授权」会在后台打开一次性授权页；手动执行 `auth codebuddy` 时也可加 `--open-browser`。不会把令牌写入项目或日志。

Windows 下项目会优先使用 `.agent-hub/tools/` 中固定版本的官方 CodeBuddy CLI。真实 PoC 的 Worker 只获得 `StructuredOutput` 工具；模型返回结构化内容后，由 Adapter 严格按声明路径和精确字节契约落盘，再由 Git Manager 校验实际 diff。Codex 运行强制并验证 `chatgpt` 登录方式，发现 API Key 认证不会继续。

在线探针会消耗少量账号用量，且默认关闭：

```powershell
.\.venv\Scripts\python.exe -m orchestrator probe --live codex
.\.venv\Scripts\python.exe -m orchestrator probe --live codebuddy
```

阶段 0 的真实能力探针也会消耗少量账号用量：

```powershell
.\.venv\Scripts\python.exe -m orchestrator spike codebuddy-sessions
.\.venv\Scripts\python.exe -m orchestrator spike codebuddy-safety
.\.venv\Scripts\python.exe -m orchestrator spike codex-lifecycle
```

运行时缓存、探针输出和 SDK 临时文件统一放在 `.agent-hub/`，不会进入 Git。
