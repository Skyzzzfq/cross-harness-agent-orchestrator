# WorkBuddy 当前整改交接单

更新时间：2026-09-01
审计基线提交：87b875e；实际接手点以远端 main 为准
阶段状态：**Stage 2 AUDIT-OPEN；Stage 3 禁止开始。**

## 1. 开始前阅读

按顺序完整阅读：

1. AGENTS.md
2. PROJECT_PROGRESS.md
3. STAGE2_AUDIT_FINDINGS.md
4. STAGE2_AUDIT_RESPONSE.md
5. 跨Harness多Agent团队编排系统实施计划.md
6. STAGE2_REPORT.md

审计问题清单和 WorkBuddy 对账回复作为不可改写的审计基线。修复状态写入 PROJECT_PROGRESS.md 和 STAGE2_REPORT.md。

## 2. 当前基线

- 审计与对账基线：87b875e；状态对齐提交以远端 main 为准。
- schema v12。
- 150 项测试通过。
- 阶段 0、阶段 1 签字有效。
- 原 d2519fe Stage 2 complete 签字已撤销。
- 已确认 3 项 P0、6 项 P1、4 项文档问题。
- 现有真实场景只证明只读 Adapter 到达 REVIEW 和三路真实重叠，不代表完整 Task 终态。

## 3. 第一个任务

只处理 P0-01：Authority 与 takeover fencing。

要求：

- 先补失败测试，证明旧 authority epoch 仍能通过当前接口派发、入队、领取或完成 merge。
- 派发、审核、入队集成、领取集成和完成集成都必须原子校验当前 AuthorityToken。
- ACTIVE authority 不得被普通 acquire 无条件覆盖。
- 强制接管必须是单独、人工审批、可审计的恢复流程。
- 旧 epoch 的每个主管动作必须零副作用。
- 不要在本切片顺便修改路径、Merge/Outbox 或 Adapter。

完成后：

- 运行全量测试。
- 更新 PROJECT_PROGRESS.md 和 STAGE2_REPORT.md。
- 提交信息：stage2: checkpoint enforce authority and takeover fencing。
- 推送并记录远端 SHA。

## 4. 后续顺序

1. P0-03：canonical workspace 和 write_scope。
2. P0-02 + P1-06：Transactional Merge、Git、Outbox 和重试。
3. P1-02 + P1-03：完整预算和可消费审批。
4. P1-04 + P1-05：真实 writable Scheduler、超时、取消和脱敏。
5. P1-01：修正 terminal 口径，重跑 Fake 与真实完整退出矩阵。
6. 修正文档并重新签字 Stage 2。

不得跳步，不得提前开始 Stage 3。

## 5. 安全边界

- Codex 使用 ChatGPT Plus saved login，不要求 OpenAI API Key。
- CodeBuddy 固定中国站 internal 环境。
- 不读取、打印、提交 token、cookie、credential 或 Session secret。
- 运行状态、工具和报告只放 .agent-hub/。
- 写任务只能进入受管 worktree；用户 checkout 不得修改。
- 失败证据不得删除或挑样。

## 6. 接手核验

在项目根目录执行：

    & '.venv\Scripts\python.exe' -m unittest discover -s tests -v
    & '.venv\Scripts\python.exe' -m orchestrator status
    git status --short
    git log --oneline --decorate -15
    git ls-remote --heads origin main

如果基线与本文不符，先在 PROJECT_PROGRESS.md 记录差异，不要静默覆盖。

## 7. 提交纪律

- 修复期间只能使用 stage2: checkpoint ...。
- 每次行为变化后运行全量测试。
- 每个切片同时更新 PROJECT_PROGRESS.md 和 STAGE2_REPORT.md。
- 只有全部审计问题关闭、完整退出条件重新通过且真实证据齐全后，才能创建新的 stage2: complete ...。
