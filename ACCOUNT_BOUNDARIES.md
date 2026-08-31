# 阶段 0 账号与费用边界

更新时间：2026-08-31

## 拟采用边界

| 执行端 | 登录与计费路径 | PoC 并行上限 | 达到限额后的行为 |
|---|---|---:|---|
| Codex | ChatGPT Plus 保存登录；不用 API Key | 1 个主管任务 | 暂停，等待用量窗口恢复 |
| CodeBuddy | 中国站官方 CLI 保存登录 | 2 个 Worker | 暂停，不自动扩容或购买 |

## 依据

- Codex 已通过 Plus 登录完成真实只读调用、Session 恢复、失败、超时和取消验证。
- OpenAI 官方文档说明 Plus 包含 Codex，并支持 CLI、SDK 和脚本化工作流；ChatGPT Work 与 Codex 共享套餐用量。API Key 是独立的按 Token 付费路径：[官方定价与用量说明](https://learn.chatgpt.com/docs/pricing)。
- CodeBuddy 已在中国站完成两个 Session 的真实并发验证；上下文、Session ID 和工作目录相互隔离。
- 腾讯公开资料没有给出当前个人账号的 CodeBuddy Code CLI 精确并发上限、速率限制和按次价格，因此只承诺已经实测的两路并发，不外推更高容量。
- 任一执行端返回限流、额度不足或付费提示时，Orchestrator 必须停止派发，不得自动购买积分、credits 或切换 API 付费。

## 接受记录

- 状态：**已确认**
- 确认日期：2026-08-31
- 已接受内容：`1 Codex supervisor + 2 CodeBuddy workers` 的首版上限，以及“达到限额即暂停、不自动付费”的策略。
