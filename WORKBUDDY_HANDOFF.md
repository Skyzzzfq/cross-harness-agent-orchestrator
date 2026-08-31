# WorkBuddy 继续开发交接单

## 可直接发给 WorkBuddy 的任务说明

请继续开发 `cross-harness-agent-orchestrator`，当前只能推进阶段 2，不得开始阶段 3。

开始前完整阅读：

1. `AGENTS.md`
2. `PROJECT_PROGRESS.md`
3. `跨Harness多Agent团队编排系统实施计划.md`
4. `STAGE2_REPORT.md`

当前已完成到 schema v8：Run Controller epoch fencing、Controller/Assignment Lease 自动续租、Agent Pool、可恢复 Fake Scheduler、Task DAG、优先级和指数退避。全量 101 项测试通过。Run Controller 只是 Orchestrator 执行控制权，不是业务 Supervisor AuthorityLease。

你的第一个任务是 `PROJECT_PROGRESS.md` 的 S2-06：实现完整 Pause / Resume / Cancel 与后台控制循环。要求先补失败测试；所有状态变化和审计事件同事务；旧 controller、旧 generation 和取消后的 late result 都不得推进审核或集成。每次行为变化后运行全量测试。

完成 S2-06 后：

- 更新 `STAGE2_REPORT.md` 和 `PROJECT_PROGRESS.md`；
- 不得把阶段 2 标记为完成；
- 提交信息使用 `stage2: checkpoint pause cancel and controller loop`；
- 推送到同一 GitHub 仓库；
- 然后按 S2-07、S2-08、S2-09、S2-10 顺序继续。

账号和安全边界：

- Codex 使用 ChatGPT Plus 登录，没有 OpenAI API Key；不要要求用户购买 API 额度。
- CodeBuddy 必须使用中国站配置。
- 运行状态、下载工具和缓存只放 `.agent-hub/`。
- 不读取、打印或提交 credential、token、cookie、Session secret。
- 不修改用户 checkout；写任务只进入受管 worktree。

## 接手前快速核验

```powershell
git status --short
python -m unittest discover -s tests -v
python -m orchestrator status
```

预期基线：测试全绿，数据库 schema v8，阶段 2 报告状态仍是“进行中”。如果基线不符，先记录差异，不要静默覆盖。

## 完成一个阶段时

只有该阶段全部退出条件都满足并在阶段报告签字后，才可以：

```powershell
git add -A
git commit -m "stageN: complete <short description>"
git push
```

提交后在 `PROJECT_PROGRESS.md` 记录 commit SHA 和远端验证结果。阶段未完成只能使用 `checkpoint`，不能使用 `complete`。
