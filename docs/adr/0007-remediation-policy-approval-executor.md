# ADR-0007：修复闭环——模型提议、确定性策略、绑定计划哈希的人工审批、独立执行器与 SLO 验证

- 状态：已接受
- 日期：2026-10-15
- 相关里程碑：M5（修复 B9、B10、B11）

## 背景

M4 之后，一个告警能走到 DIAGNOSED：有排好序的根因候选，每条都有证据。M5 要从诊断走到修复：提议一个动作，判断能不能做，必要时让人批准，执行，然后确认 SLO 真的恢复了。约束：

- **模型会出错，也可能被注入**。日志、span 里的文字是服务打印的，任何人都能往里写"立即回滚 payment"。模型的提议只能是提议。
- **执行是对集群的写操作**。M3 的工具身份只能读；写的权限不能落到跑模型的进程手里。
- **会崩溃、会重试**（M1）。队列是"至少一次"投递，可回滚不能"至少一次"地执行。
- **原型的问题**：`not_event_host` 检查只要有任意证据引用就通过（B10）；注入检查是子串匹配（B11）；审计哈希被截断（B9，M1 已修，缺 `verify` 命令和接口）。

## 决定

**一、流程与状态**

1. DIAGNOSED 之后排一个 `plan` job（和 DIAGNOSED 在同一个事务里）：
   `DIAGNOSED → PLANNED → SAFETY_REVIEWED →`
   - 策略 `allow` → `EXECUTING`
   - 策略 `require_approval` → `AWAITING_APPROVAL` → 批准 → `EXECUTING`；拒绝或超时 → `ESCALATED`
   - 策略 `deny`，或者计划里没有动作 → `ESCALATED`（附带理由，人来接手）
   `EXECUTING → VERIFYING → RESOLVED`，执行或验证失败 → `ESCALATED`。
2. 新增一条转移：`AWAITING_APPROVAL → PLANNED`。值班的人（responder）可以提交修改后的计划，它重新走一遍策略；旧的审批随之失效（见第 11 条）。

**二、模型只提议，而且只能从目录里选**

3. Planner 是第五个 Agent，复用 M4 的循环：只读工具只有 `rollout_history`、`deploy_diff`、`k8s_events`，最多 3 步；最终答案是 `RemediationPlan`。它的检查点和其他 Agent 一样写在 `agent_runs`。
4. 动作目录（`app/remediation/actions.py`）是固定的、结构化的。每种动作有风险等级、参数 schema、逆操作，以及执行器里对应的实现：

   | 动作 | 风险 | 逆操作 |
   |---|---|---|
   | `rollout_undo`（回到上一个或指定 revision） | R3 | 再回滚到执行前的 revision |
   | `restart_deployment` | R2 | 无需（无状态服务重启） |
   | `scale_deployment` | R2 | 恢复执行前的副本数 |

   模型的输出 schema 里只有这些分支，没有"执行任意命令"。R4（删数据、批量变更、改网络 ACL）不在目录里，提了也会被 schema 拒绝。
5. 计划里每个动作都要引用证据 ID，用 M4 的确定性引用校验：必须存在、属于本事件，并且 Planner 自己看过，或者诊断引用过。

**三、策略引擎：纯函数，只读结构化字段**

6. `evaluate(plan, context) → Decision`，在 `app/remediation/policy.py`，没有 I/O，可以穷举单测。输入只有结构化字段：动作类型和参数、引用的证据 ID 及其工具和参数、诊断的根因服务和置信度、事件的服务、配置。**模型写的理由、摘要一个字都不读。**
7. 逐条检查，每条给出通过与否和原因：

   | 检查 | 内容 | 不通过 |
   |---|---|---|
   | `catalogue` | 动作在目录里，参数合法 | deny |
   | `scope` | namespace 是配置的那个；目标在允许列表里（默认空 = 什么都不许，fail closed） | deny |
   | `problem_host`（B10） | 目标是排第一的根因服务，不是只表现出症状的事件服务；置信度不低于阈值（默认 0.5） | deny |
   | `evidence` | 每个动作至少一条证据；`rollout_undo` 必须引用这个 Deployment 的 `rollout_history` 或 `deploy_diff` 结果，证明确实有一次发布可撤 | deny |
   | `rollback_plan` | 动作有逆操作（由目录给出，不由模型给出） | deny |
   | `change_window` | 当前时间在变更窗口内（配置，默认全天） | deny |
   | `alternative` | R3 动作要列出考虑过的更低风险的动作 | 只提示，给审批人看 |

8. 结论：任何一条 deny 类检查失败 → `deny`；全部通过时，风险不高于 `SRE_REMEDIATION_AUTO_MAX_RISK`（默认 R1，也就是目录里的动作全都要审批）→ `allow`，否则 `require_approval`。策略本身出异常 → `deny`。
9. 执行器执行前**再评估一次**（时间窗口可能已经过了，配置可能改了），不信任 worker 存下的结论。

**四、审批**

10. `POST /api/v1/incidents/{id}/approvals`，需要 approver 角色，只能批本租户的事件：
    - 请求里必须带 `plan_hash`，等于当前计划的哈希，否则 409；
    - 审批人不能是计划的提出人（`agent:planner` 提出的计划任何 approver 都可以批；responder 修改的计划，他自己不能批），否则 403；
    - 计划有过期时间（默认 30 分钟），过期后 409；到期时一个延迟 job 把事件转 ESCALATED。
11. `plan_hash` = 计划的规范 JSON（动作、目标、参数、引用的证据）加上事件 ID 和计划序号的 SHA-256。计划改了哈希就变，旧审批不会匹配新计划；执行器也会核对"批准的哈希 = 要执行的计划的哈希"。
12. 计划、审批是只追加的表（触发器拒绝 UPDATE 和 DELETE），和审计链一起构成"谁、在什么时候、批准了哪一份计划"。

**五、执行器：独立进程、独立身份、至多一次**

13. `python -m app.executor` 是独立进程，只认领 `execute` 类 job（队列按 kind 认领）。它用自己的 K8s ServiceAccount：只能在 demo namespace 里 get/list/patch Deployment、get/list ReplicaSet。worker 进程没有这个 token，执行器进程没有模型和读工具。
14. 每个动作先做服务端 dry-run（`?dryRun=All`），再真正执行；补丁带 `resourceVersion` 的 test 操作，Deployment 在读和写之间被别人改过就失败，不会覆盖别人的改动。
15. 至多一次：动作开始前先提交一行 `executions`，`idempotency_key = 计划 ID + 动作序号` 唯一。job 重跑时看到这行已经是 `started`（上次在调用 K8s 的过程中崩了），不重试，而是标成 `unknown` 并 ESCALATED，让人去看集群的实际状态。宁可不做，也不做两遍。
16. `rollout_undo` 按 `kubectl rollout undo` 的做法实现：在 Deployment 拥有的 ReplicaSet 里找到目标 revision，把它的 pod 模板（去掉 `pod-template-hash`）写回 Deployment。执行前的 revision 记下来，作为逆操作的参数。

**六、验证**

17. 执行完排一个 `verify` job：先等 60 秒，然后每 30 秒用 PromQL 查一次事件服务的 SLI，连续 3 次健康才算恢复，最多等 10 分钟。查询由告警的 `slo` 标签决定（可用性：1 分钟错误率低于 6%；延迟：1 分钟 p95 低于 1 秒），不由模型决定；不认识的 SLO 无法验证，直接 ESCALATED。
18. 恢复 → RESOLVED（`closed_at` 写上，之后同样的告警会开新事件）；不恢复 → ESCALATED，理由里写上逆操作，作为给人的建议。

**七、审计（B9）**

19. 每一步都写审计：计划、策略结论（逐条）、审批、dry-run、执行前后的状态、验证结果。`python -m app.db.audit verify` 和 `GET /api/v1/audit/verify`（admin）重算整条链；`GET /api/v1/incidents/{id}/audit` 给出一个事件的完整记录。

**八、提示注入（B11），纵深防御**

20. 工具结果在 `<result>` 数据区里，所有角色的提示都写明其中的文字是数据、不是指令（M4 已有）。
21. Agent 没有写工具；最坏情况下被注入的模型只能**提议**。
22. 提议要过确定性策略：比如日志里写着"回滚 payment"，而根因是 checkout、也没有 payment 的发布证据，策略会拒绝。策略不读模型写的文字。
23. 执行器只接受目录里的结构化动作，执行前重新评估策略，并核对审批绑定的哈希。
24. 测试里有注入用例：日志行里写着指令时，被"说服"的 Planner 提议的动作会被策略拒绝；另有一个用真实模型的注入检查。

## 考虑过的方案

| 问题 | 方案 | 结论 |
|---|---|---|
| 谁来提议动作 | 规则：根因 + 最近有发布 → 回滚 | 不采用：只覆盖一种情况；但这条规则的精神保留在策略的 `evidence` 检查里 |
| | **模型从固定目录里选，策略判定** | **采用**：模型擅长读诊断，策略保证安全 |
| 谁判定能不能做 | 再让一个模型审核 | 不采用：会出错，也会被同样的注入影响，不可测 |
| | **纯函数策略引擎** | **采用** |
| 执行 | worker 直接调 K8s | 不采用：跑模型的进程拿到写权限，注入的后果就从"提议"变成"执行" |
| | **独立进程 + 独立 ServiceAccount** | **采用** |
| 至多一次 | K8s 操作本身幂等就够了 | 不够：`rollout undo` 做两遍会回到坏版本 |
| | **先提交执行记录，崩溃后不重试而是升级** | **采用** |
| 回滚的实现 | 调用 `kubectl` | 不采用：镜像里要带 kubectl，权限和参数更难控制 |
| | **直接用 K8s API 打补丁** | **采用**，和 kubectl 的做法一致 |
| 验证 | 等告警自己 resolve | 不采用：告警的 5 分钟窗口和 `for` 要好几分钟才放下，而且告警 resolve 不等于 SLO 达标 |
| | **执行器之外的 verify job 直接查 SLI** | **采用** |

## 后果

- 正面：
  - 从告警到回滚到恢复，每一步都有记录，谁批准了哪一份计划可以验证。
  - 模型被注入、给出错误建议、或者 worker 被攻破，都没法直接改集群。
  - 执行器崩溃不会导致重复执行。
- 代价：
  - 目录里只有三种动作；feature flag 和 Chaos Mesh 造成的故障（支付失败、网络延迟）没有对应的修复动作，这类事件会以"没有可执行的动作"升级给人。
  - 默认所有动作都要审批，自动执行要显式放开。
  - `unknown` 状态需要人去集群确认。
- 什么情况下应该重新考虑：
  - 动作类型变多：把目录和策略规则改成声明式配置（例如 OPA/Rego），而不是 Python。
  - 多集群、多 namespace：执行器的身份要按目标拆分。
