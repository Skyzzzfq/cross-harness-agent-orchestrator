# T8：8 Agent 验证 + MCP Facade / native team 评估（timebox）

更新时间：2026-09-02
范围：实施计划阶段 3 的"可选验证到 8 个 Agent"与"MCP Facade 和 CodeBuddy Code native team
模式用不超过 1–2 天的非阻断 timebox 评估"。默认并发仍保持 2–4；8 Agent 是可选上限验证。

## 1. 8 Agent 并发验证（✅ 已通过）

`tests/test_stage3_eight_agents.py`：

- **并发峰值**：8 个 agent 的 pool 在 16 个 Fake 任务下并发峰值达到池上限 8（BUSY=8），
  证明调度并行而非串行。
- **8 路写任务闭环**：8 个不重叠 write_scope 写任务并行 → REVIEW → 真实 Git 集成 →
  全部 COMPLETED，merge_queue 恰好 8 条 APPLIED（0 重复）。

结论：编排器支持 8 Agent 并发，写任务闭环正确。默认并发建议仍为 2–4（真实厂商成本/限流），
8 Agent 仅在有资源时启用（`AgentPoolSpec.count=8`）。

## 2. MCP Facade 评估（timebox 结论）

范围：为每个 Agent 提供统一工具/资源访问 facade 的价值与成本。

评估结论（1 天 timebox，非阻断）：

- **不阻塞 MVP/Beta**：当前编排模型里，agent 的工具集由各厂商 SDK 原生管理（Codex sandbox /
  CodeBuddy 权限模式），对外暴露统一 MCP facade 属于增强，不是正确性依赖。
- **潜在价值**：统一能力声明（BackendCapabilities）、共享工具（搜索/文档/回测）、
  策略化权限在 facade 层落地。
- **成本**：需要新增一层进程/服务管理 + 工具注册 + 权限映射，与现有 WorkspacePolicy/
  authority 体系叠加复杂度。
- **建议**：延后到下一阶段；若需要，先做"能力协商 + 工具白名单"的只读 facade 原型。

## 3. CodeBuddy Code native team 模式评估（timebox 结论）

范围：CodeBuddy Code 自带的 native team 模式能否替代/增强本编排器的角色模型。

评估结论（1 天 timebox，非阻断）：

- **不建议采用为运行时依赖**：native team 的角色/权限语义与本项目"跨厂商统一"目标不符
  （Codex 没有等价物），接入会把厂商耦合进编排核心。
- **仅作为被编排对象**：CodeBuddy Code 作为 worker 时，本编排器用其 SDK（query/cancel）
  已覆盖所需行为（含 cancel_unconfirmed 隔离）。
- **建议**：不做 native team 集成；保持 CodeBuddy 作为受控 worker 角色。

## 4. 总体结论

- 8 Agent 并发验证通过，写任务闭环正确。
- MCP Facade 与 CodeBuddy native team 均为**非阻断、延后**项；当前架构不依赖它们。
- 默认并发 2–4；8 Agent 为可选上限，测试已验证可行。
