# Cross-Harness Agent Team Orchestrator

这是一个本地多 Agent 编排器，目标是让 Codex 与 CodeBuddy/WorkBuddy 在同一项目中承担可配置的主管、执行和审核职位。

当前状态：**阶段 0 已 GO；阶段 1 已通过；阶段 2（MVP）已整改完成并重新签字 PASS；阶段 3 待开始。** 审计发现的 3 项 P0 与 MVP 必需 P1 已全部修复（authority fencing、workspace 边界、merge/git/outbox 原子闭环、真实写任务、超时/脱敏、终态口径），全量 194 项测试通过。剩余【Beta 再补】项（金额预算、审批原子消费、Outbox 重试）转入阶段 3。

当前状态入口：[PROJECT_PROGRESS.md](PROJECT_PROGRESS.md)。审计原文与 WorkBuddy 对账分别见 [STAGE2_AUDIT_FINDINGS.md](STAGE2_AUDIT_FINDINGS.md) 和 [STAGE2_AUDIT_RESPONSE.md](STAGE2_AUDIT_RESPONSE.md)。阶段 3 开始前先读 [WORKBUDDY_HANDOFF.md](WORKBUDDY_HANDOFF.md)。

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
.\.venv\Scripts\python.exe -m orchestrator demo --fake
.\.venv\Scripts\python.exe -m orchestrator demo --git-fake
.\.venv\Scripts\python.exe -m orchestrator demo --recovery-fake
.\.venv\Scripts\python.exe -m orchestrator demo --real
.\.venv\Scripts\python.exe -m orchestrator probe
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

`init` 会校验 [团队配置](config/team.yaml)，并把运行状态初始化到 `.agent-hub/state/agent-hub.db`。配置文件采用 JSON-compatible YAML 1.2，以便编排核心继续只依赖 Python 标准库。

`status --run` 提供隔离的单 Run 只读汇总；`reconcile` 执行一次显式状态协调，只回收已过期且仍为 ACTIVE 的 Assignment Lease。恢复时会再次按 generation、状态和同一 cutoff 做条件更新，避免扫描后 Worker 已续租却被误回收。当前已有后台常驻控制循环，但 CLI `serve` 仍只注册 Fake Adapter，真实 Adapter、Review、Git Merge 和 Outbox 尚未接入同一常驻闭环。

`demo --fake` 不调用在线模型，用于验证两个 Worker 的并行、Reviewer 驳回和新 Attempt 返工。运行报告写入 `.agent-hub/reports/`。

`demo --git-fake` 在 `.agent-hub` 的隔离仓库中验证并行 worktree、串行集成、确定性测试和同路径冲突阻断；`demo --recovery-fake` 会启动并强制终止可控子进程，验证租约回收、重排、明确失败和旧代结果 fencing。两者都不会调用在线模型或修改当前 checkout。

`demo --real` 运行固定的真实验收场景：一个 Codex Plus Thread 负责规划和审核，两个中国站 CodeBuddy Session 并行产出，B 首次提交被驳回后由新 Attempt / Session 返工。单次成功显示 `run-passed`；连续三次成功后才显示 `ready`。每次运行都会保留报告和不可挑样的验收历史。

桌面端登录不会自动共享给独立 SDK。首次使用时分别执行：

```powershell
.\.venv\Scripts\python.exe -m orchestrator auth codex
.\.venv\Scripts\python.exe -m orchestrator auth codebuddy
```

CodeBuddy 已固定使用中国站（`copilot.tencent.com`，SDK 环境 `internal`），不会跳转到国际站。命令只在终端显示厂商的一次性登录地址，不会把令牌写入项目或日志。

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
