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

## 3. 阶段 3 任务台账

1. **T1 24h 稳定运行测试框架** ✅（59448ad）：`scripts/stage3_stability_run.py` + 高密度/崩溃注入测试。
2. **T2 20 个预冻结真实场景** ✅（e5d462e）：`orchestrator/poc/stage3_scenarios.py` 20 场景清单 + Fake 验证框架。
3. **T3 本地状态页 + 管理控制台** ✅（2d955b2）：`orchestrator/console/server.py`，CLI `console`。
4. **T4 Adapter 能力协商与回归** ✅（289cd88）：`BackendCapabilities` + scheduler 能力校验 + 功能开关。
5. **T5 Windows 支持矩阵** ✅（b569778）：中文/空格/CRLF/长路径/文件锁（修复 commit_file 部分写入）。
6. **T6 数据库升级/降级/备份/恢复** ✅（0c27c10）：`db_ops.py` + CLI `db-backup/restore/verify`。
7. **T7 干净 Windows bootstrap** ✅（eaec061）：`bootstrapper.py` + `scripts/bootstrap.py` + `docs/INSTALL.md`。
8. **T8 可选 8 Agent + MCP/native timebox** ✅（631d5a0）：8 Agent 并发/写闭环 + `docs/T8_MCP_EVALUATION.md`。
9. **Beta 再补项** ✅：B1 预算（49aaea4）、B2 审批消费+重新分配（f1027df）、B3 Outbox 重试+死信（a97942c）。

全量 **248 项测试通过**，编译 OK、凭据扫描 0。

## 4. 阶段 3 剩余门槛（需真实环境执行）

代码与框架已完成；以下真实验证门槛需要 Codex/CodeBuddy 账号环境：

1. **E1**：`scripts/stage3_stability_run.py --hours 24 --tasks 600` 真实 24h 跑，完全 drain，0 丢/0 重复。
2. **E2**：`stage3-real` 跑 20 个预冻结真实场景 ≥19 正确，0 数据丢失/0 重复 merge。
3. **E3**：drain 后孤儿进程/无引用 worktree 为 0。
4. **E4**：Windows 中文/空格/CRLF/长路径/文件占用处理演练。
5. **E5**：Prompt 注入语料下越界写/凭据泄露/绕过审批/改变主管权 4 项 0。
6. **E6/E7**：升级、降级、数据库恢复演练 + 15 分钟 rollback（`db-backup`/`db-restore`/`db-verify`）。
7. **E8**：干净 Windows 30 分钟安装演示（`docs/INSTALL.md`）。

全部满足后创建 `stage3: complete Beta`。

完成后：

- 更新 PROJECT_PROGRESS.md 和 STAGE2_REPORT.md。
- 提交信息使用 `stage3: checkpoint <short description>`，不得使用 complete（阶段 3 未全部完成）。
- 推送并记录远端 SHA。
- 全程不修改审计已确认的安全边界（authority/workspace/merge 原子性）。

## 5. 后续顺序

按 T1→T2→…→T8→Beta 再补项推进；每项用 checkpoint 提交；每项行为变化后运行全量测试。

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
