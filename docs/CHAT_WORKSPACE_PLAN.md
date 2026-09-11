# Chat Workspace：项目—角色—Agent 对话工作台

## 目标

把当前“Run 下任务列表 + 只读对话”升级为类似 ChatGPT 项目工作区的本地工作台：

```text
项目（本地工作空间）
├── 主管角色
│   └── Codex Supervisor
└── Worker 角色
    ├── CodeBuddy Worker 1
    └── CodeBuddy Worker 2
```

项目是最高层级，绑定一个本地工作空间和默认团队；Run 是项目的一次执行记录，不能取代项目本身。用户选择 Agent 后进入该 Agent 的独立上下文，既能查看调度器派发的任务，也能发送用户引导，不改变主管对 Worker 的派发权。

## 界面方向

采用“编排工作台”而不是普通任务表：

- 左侧窄栏：项目列表、创建项目、选择本地工作空间。
- 左侧树：项目 → 角色 → Agent；Agent 行显示在线/忙碌/空闲/失败和未读数。
- 中央对话：用户消息、主管派发、Agent 流式反馈、系统状态按时间排列。
- 对话顶部：Agent 名称、后端、模型、当前 Run、工作空间和运行状态。
- 输入框下方提供两个明确动作：`立即引导`、`本轮结束后发送`。任务运行时默认禁用会改变权限边界的操作。
- 右侧抽屉（可选）：当前任务、尝试次数、审批、变更范围和事件时间线。

首个视觉签名是“角色轨道”：每个角色是一条细色轨道，Agent 节点沿轨道显示实时状态；颜色只表达状态，不承担权限含义。键盘焦点、窄屏折叠和 reduced-motion 必须保留。

## 数据模型

### Project

建议新增 `.agent-hub/projects/<project_id>.json`（不放凭据）：

```json
{
  "project_id": "connect",
  "name": "Cross-Harness Agent Hub",
  "workspace_root": "D:/workspace/connect",
  "team_id": "default",
  "default_run_id": "run-..."
}
```

### Agent 对话

任务派发消息和用户引导消息必须分开记录：

- `dispatch`：主管/调度器产生，保留原任务与审计链。
- `user_guidance`：用户产生，绑定 project/run/agent/call，不能修改 `access_mode`、`write_scope`、审批状态或主管权限。
- `agent_output`：后端返回的文本/结构化结果。
- `system`：排队、送达、失败、重试、超时等状态。

当前 `messages` 与 `backend_calls` 已足够支撑只读回放；下一步增加 message kind、agent_id 和 delivery 状态，避免把用户引导伪装成新的主管任务。

## 发送与引导语义

### Codex

Codex SDK 支持活动 turn 的 `turn/steer`、`turn/interrupt` 和线程恢复。需要由 serve 进程持有活动 turn 句柄，控制台通过数据库队列提交引导，serve 在 controller/authority fencing 下投递，成功后写入 `guidance.delivered`。

### CodeBuddy

CodeBuddy SDK 支持 session id、resume/continue 和 query interrupt。当前适配器是每次任务一次 query，尚未把 session 句柄暴露给控制台。因此先实现“本轮结束后发送”，再以独立切片验证活动会话引导；不能把新的独立 query 宣称为同一上下文的即时插话。

### 安全边界

- 引导只影响自然语言上下文，不扩展工作目录、写范围、工具白名单或审批权限。
- Agent 忙碌时“立即引导”失败必须可见；“本轮结束后发送”进入队列。
- serve 停止、任务取消、调用超时或 Agent 重启时，队列消息必须显示 `expired` 或 `requeued`，不能静默丢失。

## 分阶段实施

1. **Slice H1：项目与树形导航**
   - 项目文件、工作空间选择、项目→Run→角色→Agent 树。
   - 保留现有 Run 页面作为运维视图。
2. **Slice H2：Agent 对话工作区**
   - 按 Agent 聚合任务/调用/系统事件；增量轮询而不是整页刷新。
   - 用户消息先落库，支持“本轮结束后发送”。
3. **Slice H3：Codex 活动 turn 引导**
   - serve 侧 turn registry + 持久化 guidance queue。
   - `steer`、`interrupt`、排队三种状态和回归测试。
4. **Slice H4：CodeBuddy session 引导**
   - session resume/continue 验证、失败降级为队列发送。
   - 明确展示“同一会话”或“新一轮任务”。
5. **Slice H5：整体验收**
   - 3 Agent 并行反馈、主管派发不受用户聊天影响、重启恢复、权限边界、消息不丢失。

## 默认团队

当前 `config/team.yaml` 的默认团队是：

| 池 | 后端 | 角色 | 数量 |
|---|---|---|---:|
| `codex-supervisor` | Codex | `supervisor` | 1 |
| `codebuddy-workers` | CodeBuddy 中国站 | `worker` | 2 |

主管任务必须先由唯一 Codex Supervisor 处理；只有主管成功产出合法计划后，才会物化 Worker 子任务。直接创建普通任务时，可以指定 Worker 后端/模型，但不会自动变成主管派发。

## test003 复盘

当前 Run `test003` 的数据为：

- `task-9b6312ce6fb2`：CodeBuddy Worker，真实回复成功，后经人工审核进入 `COMPLETED`。
- `task-7c25557a0922`：Codex Supervisor，连续两次 120 秒后进入 `adapter-execution-error`，最终 `FAILED`。
- `task-d0c702cff0dc`：新的 Codex Supervisor 任务，因唯一主管池正在恢复/没有空闲主管，停在 `READY`。

因此截图不是“两个 CodeBuddy Worker 都失败”，而是主管先失败，后续 Worker 计划没有机会生成。根因是 Codex 适配器超时分支曾经在当前 asyncio 任务内部再次等待自身，触发 self-await 异常，调用被调度器回收为 orphaned。该问题已修复并加入回归测试；旧 Run 的历史状态不会自动改写，需重新发起主管任务验证。

