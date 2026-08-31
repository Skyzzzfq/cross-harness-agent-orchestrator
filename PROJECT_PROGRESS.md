# 项目开发进度与阶段台账

更新时间：2026-09-01
GitHub：Skyzzzfq/cross-harness-agent-orchestrator
审计基线远端 main：87b875e；当前接手点以远端 main 为准
当前结论：**阶段 0、阶段 1 已通过；阶段 2 为 AUDIT-OPEN；阶段 3 被阻断。**

本文是当前状态的唯一入口。详细验收标准以《跨Harness多Agent团队编排系统实施计划.md》为准；阶段 2 的当前事实以 STAGE2_AUDIT_FINDINGS.md、STAGE2_AUDIT_RESPONSE.md 和代码为准。

## 1. 当前阶段

| 阶段 | 状态 | 证据 | 是否允许进入下一阶段 |
|---|---|---|---|
| 阶段 0：可行性闸门 | GO | SPIKE_REPORT.md、ACCOUNT_BOUNDARIES.md | 是 |
| 阶段 1：PoC | PASS | STAGE1_REPORT.md、真实三连跑历史 | 是 |
| 阶段 2：MVP | AUDIT-OPEN | 150 项组件测试、schema v12、审计与对账报告 | **否** |
| 阶段 3：Beta | 未开始 | 无 | 否 |

阶段 2 曾在 d2519fe 被标记 complete，但只读审计发现退出条件未被端到端实现。WorkBuddy 已在 STAGE2_AUDIT_RESPONSE.md 中确认全部 3 项 P0、6 项 P1 和 4 项文档问题成立，因此原签字已撤销。

## 2. 已验证基线

- 当前远端和本地基线：87b875e，提交说明为 stage2: checkpoint audit findings and workbuddy response。
- 全量测试：150/150 通过；这些测试证明已有组件行为，不代表 Stage 2 退出门禁已通过。
- 当前实际数据库：schema v12，integrity_check=ok，foreign_key_check=0。
- 阶段 0、阶段 1 的历史签字仍有效。
- 真实 Adapter 已证明 10 个只读场景到达 REVIEW，且 2 CodeBuddy + 1 Codex 存在真实并行重叠。
- REVIEW 不是 Task 终态；现有真实报告没有覆盖审核、审批、Git 集成、Outbox 和最终 COMPLETED。

## 3. 阶段 2 已存在的组件

以下能力有代码和组件测试，但仍需按审计问题接入完整闭环：

- Reconciler、Assignment Lease、generation fencing 和恢复。
- Agent / RoleBinding / Session 生命周期，1/2/4 Fake Agent Pool。
- 持久 Backend Call、可恢复 Fake Scheduler、Run Controller epoch。
- Task DAG、优先级、退避、Pause / Resume / Cancel、后台控制循环。
- Codex / 中国站 CodeBuddy 只读 Adapter 和真实并行场景。
- Merge Queue、Outbox、IntegrationIssue、Git 幂等辅助方法。
- AuthorityLease、handoff 数据结构、三层 review 记录、approval 记录和 budget 数据结构。
- 50 个 Fake 场景测试与 10 个真实只读场景报告。

这些组件不能被描述为完整 Transactional Outbox、完整业务 Authority fencing、真实写任务闭环或完整 MVP 终态。

## 4. 当前未修复漏洞

权威详情：STAGE2_AUDIT_FINDINGS.md。WorkBuddy 对账：STAGE2_AUDIT_RESPONSE.md。

### P0

- [ ] P0-01：AuthorityToken 接入派发、审核、Merge 入队/领取/完成；禁止无条件接管 ACTIVE authority。
- [ ] P0-03：canonical cwd、受管 worktree、Windows 安全 write_scope 和固定证书缓存根。
- [ ] P0-02：Merge Queue、真实 Git、数据库状态与 Transactional Outbox 的可恢复闭环。

### P1

- [ ] P1-01：修正 REVIEW 被当作 terminal 的统计，并重跑完整真实终态矩阵。
- [ ] P1-02：补齐 turn、Token、金额预算和并发预算预留。
- [ ] P1-03：审批 scope/params/expiry/single-use 原子消费，并实现重新分配。
- [ ] P1-04：真实写 Adapter 接入受管 worktree 和常驻 Scheduler。
- [ ] P1-05：超时/取消不确定性、Session 隔离和持久化前统一脱敏。
- [ ] P1-06：Outbox 持久 claim、退避重试和死信处理。

## 5. 修复顺序

不得跳过前项门禁：

1. stage2: checkpoint enforce authority and takeover fencing
2. stage2: checkpoint canonical workspace and write scopes
3. stage2: checkpoint transactional merge git and outbox
4. stage2: checkpoint approval and complete budget gates
5. stage2: checkpoint real writable scheduler and cancellation
6. stage2: checkpoint corrected exit matrix and handoff records
7. 所有退出条件重新通过后，再创建新的 stage2: complete 提交。

每次行为变化必须先补失败测试并运行全量测试；每个切片同时更新本文和 STAGE2_REPORT.md。

## 6. 重新签字 Stage 2 的最低条件

- 所有 P0、P1 关闭并有回归测试。
- 旧 authority epoch 对派发、审核和集成均为零副作用。
- cwd/write_scope 不允许项目外访问或 Windows 路径别名绕过。
- Git、数据库、Outbox 在所有关键崩溃点最终一致，0 虚假 COMPLETED、0 重复 merge。
- 真实任务经过 review、approval、integration 到达真实终态，不能只停在 REVIEW。
- turn、Token、金额和其他硬预算达到上限后零新增调用。
- 一次性审批只能被指定 Task / Attempt / 参数消费一次。
- 常驻服务能运行真实 Codex / 中国站 CodeBuddy；写任务只进入受管 worktree。
- 更新 STAGE2_REPORT.md，并提供真实报告、数据库完整性、Git 对账和失败注入矩阵。

## 7. 阶段 3

阶段 3 当前禁止开始。只有新的 Stage 2 签字提交和远端 SHA 完成核对后，才能进行 24 小时 Fake 稳定运行、20 个真实场景、状态页、Windows 矩阵和数据库回滚演练。

## 8. 文件权威顺序

1. AGENTS.md
2. PROJECT_PROGRESS.md
3. STAGE2_AUDIT_FINDINGS.md
4. STAGE2_AUDIT_RESPONSE.md
5. 跨Harness多Agent团队编排系统实施计划.md
6. STAGE2_REPORT.md
7. WORKBUDDY_HANDOFF.md
8. 代码、测试和本地 .agent-hub 运行证据

SPIKE_REPORT.md 和 STAGE1_REPORT.md 为只读历史签字。旧的 Stage 2 complete 记录只代表历史提交，不再代表当前有效阶段状态。

## 9. Git 记录

- d2519fe：历史 stage2 complete 提交，签字现已撤销。
- 5e2cc2e：历史文档一致性修正。
- 87b875e：提交审计问题清单和 WorkBuddy 对账回复；远端 main 已核验。
- 本轮状态对齐记录在包含本文的 checkpoint 提交中；最终 SHA 以 `git log` 和远端 `main` 为准。
