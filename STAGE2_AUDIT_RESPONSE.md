# 阶段 2 审计对账回复报告

对账日期：2026-09-01
对账对象：`STAGE2_AUDIT_FINDINGS.md`（ChatGPT 只读交叉审计，下称"审计"）
对账人：WorkBuddy（开发执行方）
状态：**对账完成——审计全部成立，阶段 2 从 `complete` 降级为 `audit-open`，尚未开始修复。**

---

## 1. 审计基线核对（审计准确）

| 审计所述基线 | 复核结果 |
|---|---|
| 当前提交 `5e2cc2e`，远端 main 同 SHA | ✅ 一致（本地=远端 `5e2cc2e`） |
| 全量测试 150/150 通过 | ✅ 一致（`Ran 150 tests OK`） |
| schema v12 | ✅ 一致（`PRAGMA user_version=12`） |
| `integrity_check=ok`、`foreign_key_check=0` | ✅ 一致 |
| 真实报告存在：10 只读场景 REVIEW、2 CodeBuddy+1 Codex 真实重叠 | ✅ 一致（`.agent-hub/reports/`） |
| 审计前提交链 `a3f572f…d2519fe…5e2cc2e` | ✅ 一致 |

## 2. 逐项对账结论

### P0（3 项，全部确认）

**P0-01 业务 AuthorityLease 未约束派发和集成 —— 确认**
代码证据：
- `claim_ready_dispatch()` 签名只接收 `controller: ControllerToken`，无 `AuthorityToken`：`orchestrator/storage/sqlite_store.py:836`
- `enqueue_merge()` / `claim_merge_queue()` / `finish_merge()` 同样只做 Run Controller fencing，不校验业务主管 epoch：`sqlite_store.py:1497 / 1558 / 1609`
- `acquire_authority()` 在已有 ACTIVE lease 时无条件替换 owner 并递增 epoch，不要求旧 token、租约过期、已接受 handoff 或人工接管审批：`sqlite_store.py:1846-1867`
- authority 测试仅覆盖"旧 token 无法 renew / 无法记录 review decision"（`test_review_decision_fenced_by_stale_authority`），没有覆盖旧主管无法 claim / enqueue / finish。

**P0-02 Merge Queue、Git 和 Outbox 没有形成原子闭环 —— 确认**
代码证据：
- `enqueue_merge()` 只校验 `result_commit`/`base_commit` 非空，不验证 Task 处于 REVIEW、Attempt 存在且已审核、三层审核通过、commit 属于受管 worktree：`sqlite_store.py:1508-1556`
- `finish_merge(..., "applied")` 直接将 Task REVIEW→INTEGRATION→COMPLETED，**不执行任何真实 Git integrate**：`sqlite_store.py:1658-1669`
- `record_outbox_intent()` 与 `finish_merge()` 是两次独立事务；测试也是分两次调用，非 Transactional Outbox。
- `serve()` 常驻循环只做 `reconcile_once → scheduler_tick → reconcile_pool_once`，不消费 Merge Queue、不执行 Git integrate、不投递 Outbox：`orchestrator/serve.py:77-87`
- `finish_merge()` 的 UPDATE 无 `status='APPLYING'` 条件，重复以 `conflict` 调用会重复 INSERT integration_issues 并追加事件：`sqlite_store.py:1650-1696`

**P0-03 cwd 和 write_scope 缺少统一的规范化边界 —— 确认**
代码证据：
- `AccessPolicy.__post_init__()` 只要求 `cwd` 非空，不校验其属于项目根或受管 worktree：`orchestrator/adapters/contracts.py:79-80`
- `_validate_task_spec()` 同样只校验 cwd 非空，`create_task()`/`create_task_graph()` 可保存任意 cwd：`sqlite_store.py:3730-3731`
- `codex_transport_environment()` 在传入 cwd 下创建 `.agent-hub/certs/windows-roots.pem`，未验证的 cwd 可能导致项目外写入：`orchestrator/platform.py:30-32`
- `_write_scope_conflicts()` 只做 `set(scope) & set(other)` 字符串交集，未绝对化、大小写归一、`..` 消解、目录包含、symlink/junction 检查：`sqlite_store.py:1490-1494`
- access_mode=write 允许空 `write_scope`（默认 `()`）：`contracts.py:74`

### P1（6 项，全部确认）

| 编号 | 审计问题 | 代码证据 | 确认 |
|---|---|---|---|
| P1-01 | 真实"10/10 终态"实际只到 REVIEW | `stage2_real.py:336` 把 REVIEW 计入 `scenarios_terminal`；`TaskState.REVIEW` 非终态（`state_machine.py:24-28` 可转 READY/INTEGRATION/COMPLETED） | ✅ |
| P1-02 | 硬预算未覆盖 turn/Token/金额 | `budget_status()` 仅统计 calls/tasks/run_seconds，`max_turns`/`max_cost_decimal` 只返回不参与超限判定：`sqlite_store.py:2387-2407` | ✅ |
| P1-03 | 审批只是记录，非可消费门禁 | `create_approval_request()` 无 AuthorityToken：`sqlite_store.py:2108`；`decide_approval()` 自由文本 `decided_by`：`sqlite_store.py:2164`；**无 `consume_approval()` 方法**（grep 确认）；CLI 无重新分配命令 | ✅ |
| P1-04 | 真实 Adapter 不支持写任务 | Codex 固定 `Sandbox.read_only`：`real.py:110`；CodeBuddy 固定 `permission_mode="plan"`：`real.py:282`；`_run_serve()` 只注册 `{"fake": FakeBackendAdapter()}`：`cli.py:240`（注释声称 S2-07 接入真实 Adapter，实际未接） | ✅ |
| P1-05 | 超时/取消/敏感错误信息风险 | Codex `wait_for(shield(turn.run()))` 超时后 `_finish(TIMED_OUT)` 未设 `backend_may_still_run=True`：`real.py:153-161`；`str(exc)[:500]` 直写 Failure 被持久化：`real.py:169,369` | ✅ |
| P1-06 | Outbox 失败后不自动重试 | `claim_outbox()` 只领 `status='PENDING'`：`sqlite_store.py:2479-2488`；`finish_outbox("failed")` 后无退避重试/死信 | ✅ |

### DOC（4 项，全部确认）

| 编号 | 问题 | 确认 |
|---|---|---|
| DOC-01 | `WORKBUDDY_HANDOFF.md` 过期（schema v8/101 测试/S2-06 起点/不得标完成） | ✅ 实测确认 |
| DOC-02 | `PROJECT_PROGRESS.md` 开头"已完成"与中间"阶段 2 尚未完成"标题矛盾 | ✅ 实测确认（L106） |
| DOC-03 | 完成提交 `d2519fe`、文档修正 `5e2cc2e` 的 SHA 与远端核验未写入台账 | ✅ 实测确认 |
| DOC-04 | 签字措辞超过现有证据 | ✅ 与 P0/P1 确认一致 |

### 供应链（部分确认 / 未独立复核）

- `pyproject.toml` 固定 `openai-codex==0.147.0`、`codebuddy-agent-sdk==0.3.248`，无锁文件 —— ✅ 确认。
- 未装 `pip-audit`，本次未做自动化 CVE 全量扫描 —— ✅ 确认。
- GHSA-w5fx-fh39-j5rw 影响 0.2.0~0.38.0、0.39.0 修复、当前 0.147.0 不在范围内 —— **未独立复核公告链接**，采纳审计结论但不作为"无漏洞"证明。
- CodeBuddy SDK 无官方"无漏洞"公告，搜索无结果不能当安全证明 —— ✅ 认同。

## 3. 文件权威性清单（交付本报告时的依据）

### A. 权威 / 以这些为准

| 文件 | 角色 | 备注 |
|---|---|---|
| `STAGE2_AUDIT_FINDINGS.md` | 审计原始问题清单 | 本次审计后事实的基准；修复前不得删除或改写 |
| `STAGE2_AUDIT_RESPONSE.md` | 本对账回复 | 每项审计的确认/证据/修复计划 |
| `跨Harness多Agent团队编排系统实施计划.md` | 原始设计 + 切片验收标准 | 规划的权威源，未被审计推翻 |
| `orchestrator/`、`tests/`、`config/team.yaml`、`pyproject.toml` | 实现 / 测试 / 配置 | 代码是最终事实源 |
| `AGENTS.md` | 开发纪律 | 有效，继续适用 |
| `.agent-hub/state/agent-hub.db*` | 实际运行数据库 + 逐级迁移备份（v6/v8/v9/v10/v11） | 本地运行证据，不进 Git |
| `.agent-hub/reports/run-stage2-real-*.json`、`run-stage2-mixed-*.json` | S2-07 真实场景报告 | 本地运行证据，不进 Git |

### B. 历史完成 / 只读参考（不再变更，但可引用）

| 文件 | 角色 | 备注 |
|---|---|---|
| `SPIKE_REPORT.md` | 阶段 0 签字证据 | 只读历史 |
| `STAGE1_REPORT.md` | 阶段 1 签字证据 | 只读历史 |
| `ACCOUNT_BOUNDARIES.md` | 账号边界约定 | 仍有效 |
| `README.md` | 项目介绍 | 可引用 |

### C. 过期 / 不作为状态依据

| 文件 | 原因 | 处置 |
|---|---|---|
| `WORKBUDDY_HANDOFF.md` | S2 开始时的交接单：写 schema v8/101 测试/S2-06 起点/"不得标完成"。S2 已完成且审计已开，全部过时 | **禁止作为开发依据**；保留为历史交接单，待修复完成后由新交接单取代 |
| `PROJECT_PROGRESS.md` 的"阶段 2 尚未完成：WorkBuddy 应按此顺序继续"标题（L106） | 历史章节标题，S2-06~S2-10 均已打勾 | 以开头"阶段 0/1/2 已完成"结论和各切片勾选为准；该标题待修复后更正 |

### D. 无价值 / 可清理（非证据）

| 路径 | 说明 |
|---|---|
| `cross_harness_agent_hub.egg-info/` | `pip install -e` 自动生成，无价值 |
| `**/__pycache__/` | Python 缓存 |
| `.agent-hub/tmp-*.log` | 后台运行临时日志残留，可清理 |
| `.workbuddy/memory/*.md` | 开发助手私有记忆，不进 Git，不构成项目证据 |

## 4. 当前状态与修复计划

- **状态**：阶段 2 从 `stage2: complete MVP` 降级为 `audit-open`。在 P0/P1 修复并重新验收前，不得进入阶段 3。
- **修复顺序**（按审计第 7 节，已采纳）：
  1. `stage2: checkpoint enforce authority and takeover fencing`（P0-01）
  2. `stage2: checkpoint canonical workspace and write scopes`（P0-03）
  3. `stage2: checkpoint transactional merge git and outbox`（P0-02 + P1-06）
  4. `stage2: checkpoint approval and complete budget gates`（P1-02 + P1-03）
  5. `stage2: checkpoint real writable scheduler and cancellation`（P1-04 + P1-05）
  6. `stage2: checkpoint corrected exit matrix and handoff records`（重跑矩阵 + 修正 DOC）
  7. 全部退出条件重新通过后，创建新的 `stage2: complete ...` 签字提交。
- **每步纪律**：先失败测试后实现；每次行为变化跑全量测试；修复期间保持 checkpoint 语义；修复完成时附真实运行报告 + 数据库完整性 + Git 对账 + 失败注入矩阵，不只给测试总数。
- **尚未开始**：本报告只做对账与记录，未修改任何代码。
