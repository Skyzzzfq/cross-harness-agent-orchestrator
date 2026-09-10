# 写任务闭环 UI 完成报告（2026-09-10）

## 一、本轮目标

补齐网页控制台的写任务闭环三件套，并校准项目进度台账：

1. **发起任务可填 write_scope**（写任务闭环入口）
2. **每 Run 自动准备受管 worktree**（写任务的安全工作区）
3. **REVIEW 后网页「通过 / 打回」按钮**（人工审核 → 真实 Git 集成 → 终态）
4. **回滚 index.html 噪音 + 校准 PROJECT_PROGRESS.md**

## 二、完成内容

### 1. 噪音回滚与台账校准
- `orchestrator/console/static/index.html`：回滚外部工具注入的 **51 处 `data-page-node-id` 噪音**（git restore，无业务改动，已确认 0 残留）。
- `PROJECT_PROGRESS.md`：校准到真实状态——**266 项测试**、**schema v13**、阶段 0/1/2/3 全部 PASS、`stage3: complete Beta` 已签字（tag `stage3-beta-complete`）。

### 2. 后端新增（`orchestrator/console/server.py`，+182 行）
| 能力 | 接口 | 说明 |
|---|---|---|
| worktree 自动准备 | `POST /api/runs/{run_id}/worktree` | 幂等：以项目仓库为受管主仓库创建 `.agent-hub/worktrees/<run_id>`；首次自动标记 `agenthub.managed=true`、注入 `.git/info/exclude` 忽略 `.agent-hub/`（保证集成前 clean 检查不被干扰） |
| worktree 状态查询 | `GET /api/runs/{run_id}/worktree` | 返回路径 / base_commit / 是否已存在 |
| REVIEW 通过 | `POST /api/runs/{run_id}/tasks/{task_id}/review` body `{decision:"approve"}` | 写任务：记录 human APPROVED → `commit_managed_changes` 产出 commit → `enqueue_merge` → `MergeExecutor` 真实集成 → COMPLETED；只读任务：直接 COMPLETED（无 git 产出） |
| REVIEW 打回 | 同上 `{decision:"rework"}` | 记录 human REWORK → `reassign_task` → READY 重新派发 |
| 写任务创建 | `POST /api/runs/{run_id}/tasks`（既有接口增强） | 支持 `write_scope`、`access_mode`、`cwd`、`timeout_seconds` |

审核操作复用既有 `_with_control`（临时 acquire controller+authority），serve 持权期间返回 409，与取消/暂停/恢复一致，不绕过 fencing。

### 3. 前端新增（`orchestrator/console/static/index.html`，+65 行）
- **发起任务表单**：类型选择（只读/写）→ 写任务自动准备 worktree 并回填 cwd → 填写 `write_scope`（逗号分隔）与 timeout。
- **worktree 状态面板**：Run 详情顶部展示路径 / base commit / 就绪状态，一键「准备受管 worktree」。
- **REVIEW 审核按钮**：任务行状态为 REVIEW 时显示「通过」「打回」；通过前有不可撤销确认。
- 修复既有 bug：Run 卡片渲染不再因缺失元素崩溃（上一轮遗留的 null 赋值）。

### 4. 测试（`tests/test_stage3_console.py`，+164 行）
新增 `ConsoleWriteLoopTests` 5 项，覆盖：
- worktree 准备幂等（重复调用不重复创建、GET 可见）
- REVIEW 打回 → READY + REWORK 落库
- REVIEW 通过（写任务）→ 真实集成 → COMPLETED + 主仓库包含产出文件 + APPROVED 落库
- REVIEW 通过（只读任务）→ COMPLETED + 零 merge 入队
- 非法 decision / 非 REVIEW 状态 → 400

## 三、验证结果

- 写任务闭环测试：**5/5 PASS**
- 全量测试：**266/266 PASS**（原 261 + 新增 5）
- 测试中修复的真实问题：
  1. worktree 幂等返回 created 标志错误（缓存命中仍报 created=True）
  2. **集成前 clean 检查失败**：`.agent-hub/` 未被忽略时 worktree 目录被 git status 视为 untracked → 注入 `.git/info/exclude` 解决
  3. 测试 DB 放项目内会污染受管主仓库 → 改放项目外

## 四、当前状态与遗留

- 本地 = 远端 = `e140d40`（本报告所属提交将作为下一提交）
- 全量 266 测试全绿；退出核验 PASS 6 / FAIL 0 / SKIP 1（E8 用户决策跳过）
- 写任务闭环已可在网页完成：**发起（填 write_scope）→ serve 执行 → 停 serve → 审核通过/打回 → 真实 Git 集成**

**遗留（非阻塞）**：
- serve 运行期间网页审核返回 409（需先停 serve 再审核），后续可考虑 serve 内置审核消费
- CodeBuddy CLI 在本 WorkBuddy 沙箱内仍不可真实执行（环境限制，质量由 E2 20/20 覆盖）
