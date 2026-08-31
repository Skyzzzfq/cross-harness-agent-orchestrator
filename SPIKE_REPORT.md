# 阶段 0 探针报告

更新时间：2026-08-31

## 当前结论

状态：**GO。阶段 0 全部退出条件已满足，可以进入阶段 1。**

已确认：

- 工作区开始时只有实施计划，现已初始化最小 Git 仓库和 Python 项目骨架。
- ChatGPT Plus 可通过本机保存的 ChatGPT 登录运行 Codex，不要求 OpenAI API Key。
- 本机 Codex 与 WorkBuddy 桌面进程均在运行，但 `codex`、`codebuddy`、`cbc` 没有加入 PATH。
- 采用 `openai-codex==0.147.0` 和 `codebuddy-agent-sdk==0.3.248` 作为首轮固定探针版本。
- 两个 SDK 均能导入，且各自包含可调用的本地执行组件。
- Codex 已通过 ChatGPT Plus 的设备授权完成独立登录；只读在线探针已成功返回预期结果。
- WorkBuddy 桌面端登录未被独立 SDK 自动识别，因此已单独完成 CodeBuddy 登录。
- CodeBuddy 认证和在线调用已固定到中国站（`internal` / `copilot.tencent.com`）；此前误开的国际站授权已取消且未使用。
- CodeBuddy 已通过官方 CLI 的“Log in via Chinese Site”完成登录；SDK 只读在线探针随后成功复用该登录，返回独立 Session 且响应匹配。
- CodeBuddy Python SDK 的独立 `authenticate()` 在未登录状态下虽能生成中国站 URL，但本机两次收到泛化的 `Authentication failed` 回调；因此首次引导改用官方 CLI，已保存登录后的 SDK 调用不受影响。
- 项目私有工具缓存中已固定 CodeBuddy CLI `2.142.0`；Python Adapter 优先使用该官方 CLI 运行时，缺失时才回退到 SDK 随包 headless 组件。
- 两个 CodeBuddy SDK Session 并发读取各自目录中的不同标记，执行区间重叠约 4.55 秒；Session ID、cwd 和结果均不同，其中一个 Session 随后成功恢复原上下文。
- CodeBuddy 的成功、无效输入失败、Orchestrator 超时和任务取消四类终态均可识别。
- CodeBuddy 根内受控写入通过官方非交互 CLI 成功完成；相邻目录的越界目标在 Adapter 预检阶段被拒绝，未启动 Backend、未创建文件。
- 当前 CodeBuddy Python SDK 的 stream-json 模式在 `glm-5.2` / `glm-5.3` 下会把 `Write` 退化为普通文本，`can_use_tool` / `PreToolUse` 不会触发；写任务暂用同版本官方 CLI 子进程，Session/只读任务继续使用 SDK。此兼容差异必须保留在 Adapter 契约测试中。
- Python SDK 改用 Windows `.cmd` 官方 CLI 运行时后，进程退出偶尔产生无害的 asyncio 管道清理警告；请求结果和退出码不受影响。
- Codex 持久 Session 已成功恢复并保留上一轮随机标记；恢复后的 Thread ID 与原 Thread ID 一致。
- Codex 能明确识别不存在 Session 的失败；Orchestrator 超时后可主动中断，SDK 返回 `interrupted` 终态；探针产生的持久任务已归档。
- Codex 生命周期探针在只读 Sandbox、拒绝审批和 `.agent-hub/spike/codex-lifecycle` 工作目录下运行。
- WorkBuddy 5.3.14 桌面端使用自身独立的 CodeBuddy `--serve --no-session-persistence` 后端；它不会自动显示或接管本项目创建的 SDK Session。PoC 直接控制界面因此固定为 CLI / 本地状态页，WorkBuddy 只作为独立的人类工作界面。
- 本机 Codex CLI 直接使用系统根证书时出现 `UnknownIssuer`；导出 Windows 公共根证书并通过 `CODEX_CA_CERTIFICATE` 临时传入后，TLS 已连通并准确返回未登录的 401。
- 探针不得读取或输出认证文件内容。

## 待验证能力矩阵

| 能力 | Codex | CodeBuddy | 阶段 0 状态 |
|---|---|---|---|
| Python SDK 导入 | 通过 | 通过 | 已验证 |
| 复用桌面端登录 | 不支持当前状态 | 不支持当前状态 | 已完成各自独立登录 |
| 只读问答成功 | 通过 | 通过 | 已验证 |
| 指定工作目录 | 通过 | 通过 | CodeBuddy 双 cwd 隔离已验证 |
| 明确失败终态 | 通过 | 通过 | 已验证 |
| 超时与取消 | 通过 | 通过 | 已验证 |
| Session 恢复 | 通过 | 通过 | 已验证 |
| 两个并发 Session 隔离 | 不适用首轮 | 通过 | 执行时间真实重叠 |
| 根内写入 / 越界拒绝 | 待定 | 通过 | CLI 根内写；Adapter 越界零调用拒绝 |

## 账号与费用边界

- Codex：本地 Plus 登录模式；不使用 OpenAI API Key。OpenAI 官方文档确认 Plus 包含 Codex，并支持 CLI、SDK 和脚本化工作流；套餐调用与 ChatGPT Work 共享用量，按滚动窗口和任务复杂度计算，而不是固定 Token 余额。API Key 是独立的按 Token 付费路径，本项目禁用。参考：[OpenAI 官方定价与用量说明](https://learn.chatgpt.com/docs/pricing)。
- Codex PoC 运行上限固定为 1 个主管任务；不自动购买 ChatGPT credits。达到套餐限额就暂停，等待窗口恢复或用户明确授权购买。
- CodeBuddy：中国站官方 CLI 登录；独立 SDK 可复用其保存的登录。真实双 Session 并发已通过，因此 PoC 上限固定为 2 个并行 Worker；超过 2 个必须另行探针验证。
- 腾讯公开页面未给出当前个人账号的 CodeBuddy Code CLI 真实并发上限、速率限制或按次价格。项目将“两路已实测”视为支持下限，不把它推断成账号上限；厂商返回限流或额度不足时立即停止，不自动购买或切换收费账号。参考：[腾讯 CodeBuddy 服务协议](https://copilot.tencent.com/agreement/)与[公开额度页面](https://copilot.tencent.com/quota/)。
- CodeBuddy 实测结果含 Token 与耗时字段，`total_cost_usd` 为 `0`；这不等于免费承诺，厂商还存在 credit / 次数口径，不得把该字段作为金额预算依据。
- 任何在线探针均须显式使用 `--live`，避免无意消耗额度。

用户已于 2026-08-31 确认 PoC 运行边界：`1 Codex supervisor + 2 CodeBuddy workers`、不自动付费、触发任一限额即暂停。

## Go / No-Go 决策

- 决策：**GO**
- 日期：2026-08-31
- 依据：七项 Go / No-Go 闸门与两项附加退出条件均已验证或形成明确边界；账号边界已由用户确认。
- 下一步：进入阶段 1 PoC 行走骨架。
