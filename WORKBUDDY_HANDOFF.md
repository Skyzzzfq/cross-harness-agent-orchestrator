# WorkBuddy 阶段 3 开发交接单

更新时间：2026-09-02
阶段状态：**Stage 2 已整改完成并重新签字（PASS）；Stage 3 待开始。**

## 1. 开始前阅读

按顺序完整阅读：

1. AGENTS.md
2. PROJECT_PROGRESS.md
3. STAGE2_AUDIT_FINDINGS.md
4. STAGE2_AUDIT_RESPONSE.md
5. 跨Harness多Agent团队编排系统实施计划.md
6. STAGE2_REPORT.md

审计问题清单与对账回复是历史基线，不可改写；修复结果已记录在 PROJECT_PROGRESS.md 和 STAGE2_REPORT.md。

## 2. 阶段 2 完成状态（重新签字）

- schema v12；全量 **194 项测试通过**；编译 OK、凭据扫描 0。
- 3 项 P0 全部关闭：P0-01 authority fencing、P0-02 merge/git/outbox 原子闭环、P0-03 workspace 边界。
- MVP 必需 P1 全部关闭：P1-01 终态口径、P1-04 真实写任务、P1-05 超时/脱敏。
- 【Beta 再补】转入阶段 3：P1-02（金额/turn 预算）、P1-03（审批原子消费+重新分配）、P1-06（Outbox 重试）。

## 3. 阶段 3 门槛（按实施计划）

1. 24 小时 Fake 稳定运行，累计至少 500 Task，0 丢任务、0 重复 merge。
2. 20 个预冻结真实场景至少 19 个正确，0 数据丢失。
3. 本地状态页、时间线、成本和诊断包。
4. Adapter 版本探测、能力协商、功能开关和模型/Prompt 回归。
5. Windows 中文、空格、CRLF、长路径、文件锁和进程树矩阵。
6. 数据库升级、降级、备份、恢复及 15 分钟 rollback 演练。
7. 干净 Windows 环境 30 分钟内完成安装和首次演示。
8. 可选验证 8 Agent；MCP Facade/native team 只做有时限评估。
9. Beta 再补项：P1-02/P1-03/P1-06。

## 4. 第一个任务

只处理阶段 3 门槛项 1：**24 小时 Fake 稳定运行（≥500 Task，0 丢任务、0 重复 merge）**。

要求：

- 先补失败测试，定义"稳定运行"验收（长时间 + 高任务量 + 崩溃注入下 0 丢/0 重复）。
- 复用现有 `serve` + Fake adapter + MergeExecutor/OutboxDispatcher 闭环。
- 不修改审计已确认的安全边界（authority/workspace/merge 原子性）。

完成后：

- 更新 PROJECT_PROGRESS.md 和 STAGE2_REPORT.md。
- 提交信息使用 `stage3: checkpoint <short description>`，不得使用 complete（阶段 3 未全部完成）。
- 推送并记录远端 SHA。

## 5. 后续顺序

按门槛项 2→3→…→9 推进；每项用 checkpoint 提交；每项行为变化后运行全量测试。

## 6. 安全边界（延续阶段 2）

- Codex 使用 ChatGPT Plus saved login，不要求 OpenAI API Key。
- CodeBuddy 固定中国站 internal 环境。
- 不读取、打印、提交 token、cookie、credential 或 Session secret。
- 运行状态、工具和报告只放 .agent-hub/。
- 写任务只能进入受管 worktree；用户 checkout 不得修改。
- 失败证据不得删除或挑样。

## 7. 接手核验

在项目根目录执行：

    & '.venv\Scripts\python.exe' -m unittest discover -s tests -v
    & '.venv\Scripts\python.exe' -m orchestrator status
    git status --short
    git log --oneline --decorate -15
    git ls-remote --heads origin main

如果基线与本文不符，先在 PROJECT_PROGRESS.md 记录差异，不要静默覆盖。

## 8. 提交纪律

- 阶段未完成只能使用 `stageN: checkpoint ...`；只有该阶段全部退出条件通过并重新验收后才能使用 `stageN: complete ...`。
- 每次行为变化后运行全量测试。
- 每个切片同时更新 PROJECT_PROGRESS.md 和 STAGE2_REPORT.md。
