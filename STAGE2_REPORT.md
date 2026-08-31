# 阶段 2 MVP 状态报告

更新时间：2026-09-01
状态：**AUDIT-OPEN。历史 complete 签字已撤销，整改尚未开始。**

## 1. 状态变更

阶段 2 曾在 d2519fe 标记完成。随后只读交叉审计发现退出条件与实现不一致，WorkBuddy 在 STAGE2_AUDIT_RESPONSE.md 中确认全部问题成立。当前不得进入阶段 3。

本报告只保留当前可信口径；历史完整描述可通过 Git 查看。漏洞详情以 STAGE2_AUDIT_FINDINGS.md 为准。

## 2. 当前可信验证

- 150 项单元、契约和 Fake 集成测试通过。
- 实际数据库 schema v12，integrity_check=ok，foreign_key_check=0。
- 真实 10 个只读 Adapter 场景均成功到达 REVIEW 并匹配 marker。
- 2 CodeBuddy + 1 Codex 真实调用存在时间重叠。
- GitHub main 已核对为 87b875e。

限制：

- REVIEW 不是 Task 终态。
- 当前实际主数据库中的 Authority、审批、审核、Merge Queue 和 Outbox 没有完整真实端到端记录。
- 测试全绿只证明被覆盖的组件行为，不能替代退出门禁。

## 3. 切片重新评估

| 切片 | 当前判断 | 说明 |
|---|---|---|
| S2-01 Reconciler | 组件通过 | 租约回收、generation fencing 和幂等恢复有测试 |
| S2-02 Agent Runtime | 组件通过 | Agent、Role、Session 和 Fake Pool 有测试 |
| S2-03 Scheduler | 组件通过 | Fake 闭环和并发 claim 有测试 |
| S2-04 Controller fencing | 组件通过 | Run Controller epoch 和长调用续租有测试 |
| S2-05 DAG / priority / backoff | 组件通过 | DAG、fan-in、级联和退避有测试 |
| S2-06 Pause / Resume / Cancel | 部分通过 | Fake 路径通过；真实超时/取消仍有 P1-05 |
| S2-07 真实 Adapter | 部分通过 | 只读调用到 REVIEW；未形成真实写任务完整终态 |
| S2-08 Merge / Outbox / Git | 未通过门禁 | API 存在，但未形成原子、常驻、可恢复闭环 |
| S2-09 Authority / approval / budget | 未通过门禁 | 数据结构存在，派发/集成 fencing、审批消费和完整预算缺失 |
| S2-10 退出矩阵 | 无效，需重跑 | REVIEW 被误计为 terminal，关键闭环未覆盖 |

## 4. 已确认开放问题

### P0

1. AuthorityLease 没有约束真实派发和集成，ACTIVE authority 可被无条件覆盖。
2. cwd/write_scope 缺少 canonical 项目边界，Windows 下存在越界访问和写入风险。
3. Merge Queue、Git、Task COMPLETED 和 Outbox 不在可恢复的一致性闭环中。

### P1

1. 真实 10/10 只到 REVIEW。
2. turn、Token、金额预算未执行。
3. approval 未原子消费，expiry/scope/params/single-use 未执行，缺少重新分配命令。
4. 真实 Adapter 和常驻 serve 没有写任务闭环。
5. Codex timeout、CodeBuddy cancel 和错误信息脱敏不完整。
6. Outbox FAILED 不重试，没有持久投递器。

## 5. 整改计划

按以下顺序推进，每项均使用 checkpoint 提交：

1. Authority 与 takeover fencing。
2. canonical workspace / write_scope。
3. Transactional merge / Git / Outbox。
4. approval 消费与完整 budget。
5. 真实 writable Scheduler 与超时取消。
6. 修正退出矩阵、真实端到端复验和文档。

## 6. 当前签字

阶段 2 当前没有有效 PASS 签字。新的签字必须满足实施计划的全部退出条件，关闭 STAGE2_AUDIT_FINDINGS.md 中所有 P0/P1，并附真实运行报告、数据库完整性、Git 对账和失败注入证据。

阶段 3：**BLOCKED**。
