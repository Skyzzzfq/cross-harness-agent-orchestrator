# 安装指南（干净 Windows 环境，约 30 分钟）

目标：在满足前置软件、账号和网络条件的干净 Windows 环境中，完成安装并做首次演示。

## 前置条件

| 软件 | 版本 | 说明 |
|---|---|---|
| Git | 任意较新版本 | <https://git-scm.com/download/win> |
| Python | >= 3.10 | 安装时勾选 "Add to PATH" |
| Codex CLI | 可选 | 需要 ChatGPT Plus saved login |
| CodeBuddy CLI | 可选 | 中国站 internal 环境 |

可选账号/网络条件：Codex 登录态或 CodeBuddy 登录态，网络可达 openai / codebuddy 中国站。

## 步骤 1：检查前置（约 2 分钟）

```powershell
& '.venv\Scripts\python.exe' scripts\bootstrap.py --check
```

输出每项 `ok`。必需项（python/git）缺失会以退出码 1 提示。

## 步骤 2：bootstrap（约 15 分钟，含依赖下载）

```powershell
git clone <repo-url> agent-hub
cd agent-hub
python scripts\bootstrap.py --root .
```

脚本会：创建 `.venv`、`pip install -e .`（含 openai-codex、codebuddy-agent-sdk）、初始化 `.agent-hub/{state,reports,backups,certs,logs}`。

> 如果前置检查通过但安装失败，多半是网络问题：请确认代理可达 `pypi.org` 与厂商端点。

## 步骤 3：初始化团队与数据库（约 2 分钟）

```powershell
& '.venv\Scripts\python.exe' -m orchestrator init
```

加载 `config/team.yaml` 并创建 `agent-hub.db`（schema v12）。

## 步骤 4：发起一个 Run（约 3 分钟）

```powershell
# 创建 run 并常驻调度（Fake 后端可离线演示；真实后端见后）
& '.venv\Scripts\python.exe' -m orchestrator run new --team ...   # 如支持
& '.venv\Scripts\python.exe' -m orchestrator serve --run run-1 --backend fake
```

## 步骤 5：打开管理控制台（约 2 分钟，另一个终端）

```powershell
& '.venv\Scripts\python.exe' -m orchestrator console --run run-1 --port 8080
```

浏览器访问 <http://127.0.0.1:8080>：

- 状态页：任务时间线、审批、Merge Queue、Agent 状态。
- 管理控制台：发起任务、取消、暂停/恢复（写操作复用 controller/authority fencing；serve 持权时自动只读）。

## 步骤 6：数据库备份演练（约 3 分钟）

```powershell
& '.venv\Scripts\python.exe' -m orchestrator db-backup --db .agent-hub\state\agent-hub.db
& '.venv\Scripts\python.exe' -m orchestrator db-verify --db .agent-hub\state\agent-hub.db
```

## 真实后端切换（可选）

- Fake：`serve --backend fake`（离线演示，无账号）。
- Codex：`serve --backend codex`（需 saved login）。
- CodeBuddy：`serve --backend codebuddy`（需中国站登录）。

真实写任务要求 cwd 位于受管 worktree（GitWorkspaceManager 注册），并由 `WorkspacePolicy` 校验。

## 验证

```powershell
& '.venv\Scripts\python.exe' -m unittest discover -s tests
```

全量测试通过后，即完成安装与首次演示。
