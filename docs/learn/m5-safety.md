# M5 学习笔记：安全与执行闭环——模型提议、策略裁决、人审批、独立执行、SLO 验证

> 对应里程碑：M5 · 相关决策：[ADR-0007](../adr/0007-remediation-policy-approval-executor.md)
> 这一阶段第一次让系统**改动**集群：诊断之后，Planner 提议一个修复动作，确定性的策略引擎裁决，人按计划哈希批准，一个只有补丁权限的独立进程执行，最后用 PromQL 确认 SLO 恢复了才关闭事件。任何一步失败都走到 ESCALATED，每一步都在哈希链审计日志里。

## 1. 这一阶段做了什么

| 类别 | 内容 |
|---|---|
| 动作目录 | `app/remediation/actions.py`：只有 3 个动作，`rollout_undo`（R3）、`restart_deployment`（R2）、`scale_deployment`（R2，1–10 副本）；每个都带逆操作说明和"需要什么证据" |
| Planner | 第 5 个 Agent（`app/remediation/planner.py`），复用 M4 的循环；只能查 `rollout_history`、`deploy_diff`、`k8s_events`，最多 3 步；输出 `RemediationPlan`（最多 2 个动作，附备选方案和不选的理由），引用同样过确定性校验 |
| 策略引擎 | `app/remediation/policy.py`：纯函数，只读结构化字段；6 项阻断检查（目录、范围、问题主机、证据、回滚计划、变更窗口）加 1 项提示（备选方案）；`allow / require_approval / deny` 附逐条理由；任何异常都是 deny |
| 计划与审批 | `remediation_plans`、`approvals` 只追加（触发器）；`plan_hash = sha256(事件, 序号, 计划)`；审批绑定哈希、审批人 ≠ 发起人、过期自动 ESCALATED；修订计划得到新序号和新哈希 |
| 执行器 | `app/executor/`：独立进程，只认领 `execute` job，用自己的 ServiceAccount（只能 get/list/patch Deployment、读 ReplicaSet）；执行前重跑策略、重查审批；先服务端 dry-run，再真正打补丁；`idempotency_key` 唯一约束保证至多一次 |
| 验证器 | `app/remediation/verify.py` + worker 的 `verify` job：按告警的 `slo` 标签选 PromQL 和阈值；连续 3 次健康 → RESOLVED，超时 → ESCALATED 并附上逆操作 |
| 审计 | 每一步一条事件（plan、approved/rejected、execution.started/succeeded、verification.passed/failed、状态转换）；`python -m app.db.audit verify` 和 `GET /api/v1/audit/verify` 校验整条链 |
| 注入防御 | 工具结果只在数据区；提示声明数据不是指令；策略看不到自然语言；执行器只接受目录里的结构化动作；`scripts/injection_eval.py` 用真实模型跑注入用例，在 `Model check` workflow 里 |
| API | `GET/POST /incidents/{id}/plans`、`POST /incidents/{id}/approvals`、`GET /incidents/{id}/audit`、`GET /audit/verify`，按角色（viewer、responder、approver、admin） |
| 实验环境 | 执行器的 SA 和 compose 服务；验收脚本扮演审批人，通过真实 API 批准坏版本的回滚，等 RESOLVED，检查审计链 |

## 2. 背景知识

### 2.1 为什么"模型提议、代码执行"还不够

M4 已经做到模型手里没有任何写工具：它只能输出结构化的提议。但"提议"本身仍然是模型写的，可能是错的、可能被注入了。M5 在提议和执行之间加了三道独立的关口：

```
Planner（LLM）  ──提议──▶  策略引擎（确定性）  ──裁决──▶  人（审批）  ──批准──▶  执行器（独立身份）
     可能被骗             只看结构化字段             看到的是同一个哈希          再查一遍策略和审批
```

每一道都不依赖前一道是对的。策略引擎不信模型的理由（它根本读不到）；人批准的是一个哈希，计划改一个字节旧批准就失效；执行器不信 worker（它自己重跑策略、重查审批），而且它的 K8s 身份只能做目录里那几件事。

### 2.2 风险等级和"自动"的边界

| 等级 | 含义 | 本项目 |
|---|---|---|
| R0 | 只读 | 所有工具（M3） |
| R1 | 通知、工单 | 还没有动作；`auto_max_risk` 默认就是 R1 |
| R2 | 重启、扩容 | `restart_deployment`、`scale_deployment` |
| R3 | 回滚、切流、改配置 | `rollout_undo` |
| R4 | 永久禁止 | 不在目录里，提了也是 deny |

裁决规则：任一阻断检查失败 → deny；否则风险 ≤ `auto_max_risk` → allow，否则 require_approval。默认只有 R1 能自动执行，也就是说目前所有修复动作都要人批准。这个阈值是配置，不是代码。

### 2.3 Problem Host 和 Event Host（B10）

告警响在哪里（Event Host）和故障在哪里（Problem Host）经常不同：payment 拒绝扣款，响的是 checkout。对 Event Host 动手是最常见的错误修复：重启 checkout 不会让 payment 好起来，只会制造第二个故障。

旧代码的 `not_event_host` 只要有任意证据引用就通过。现在的检查是：动作的目标必须是诊断里**排第一**的根因服务，而且置信度 ≥ 0.5（可配置）。诊断本身是 M4 的结构化输出，引用过确定性校验，所以这条检查读的是"有证据支撑的"根因，不是模型此刻的说法。

### 2.4 证据检查：回滚要有"一次发布"的证据

`rollout_undo checkout` 必须引用一条 `rollout_history` 或 `deploy_diff` 的证据，而且这条证据查的就是 `checkout` 这个 Deployment。证据 ID 由 M3 的证据库生成，策略引擎按 ID 查回工具名和参数。这样"回滚"就必须对应一次真实存在、被查到过的发布，而不是模型的猜测。重启和扩容不需要特定证据（它们不假设有一次坏发布）。

### 2.5 计划哈希：批准的到底是什么

```
plan_hash = sha256(json([incident_id, seq, plan]))
```

审批请求必须带上这个哈希。哈希不对（计划被修订了、被别人换了）→ 409。批准行和计划行都只追加，`approvals.plan_id` 唯一，所以一个计划只有一个决定。修订计划是插入一行新的 `seq + 1`，旧计划和它的审批原样留在表里。

审批人 ≠ 发起人（职责分离）：Planner 发起的计划，发起人是 `agent:planner`；人修订的计划，发起人是这个人，他不能批准自己的修订。审批人的身份只来自 JWT，不来自请求体（B4）。

过期：提交计划时就排一个延迟的 `approval_timeout` job，到期时如果还在 AWAITING_APPROVAL 就转 ESCALATED。不需要定时扫描。

### 2.6 至多执行一次

队列是"至少一次"的（M1：租约过期会重新投递）。执行器要把它变成"至多一次"：

1. 准备补丁（JSON Patch），第一条是 `test` 当前的 `resourceVersion`，Deployment 在这之间被人改过，补丁会被 API server 拒绝（422）。
2. 服务端 dry-run（`dryRun=All`），通过准入控制和校验，但不落盘。
3. **提交**一行 `executions(status='started', idempotency_key=...)`。
4. 真正打补丁，等 rollout 完成，把这行改成 `succeeded` 或 `failed`。

如果执行器在第 3 步之后被杀，job 会被重新投递，但第 3 步的唯一约束让第二次看到一行 `started`：它不知道补丁到底打没打上，所以**不重试**，把这行标成 `unknown`，事件转 ESCALATED，交给人。PATCH 请求本身的网络错误也一样（请求可能已经到了 API server）。宁可多叫一次人，也不要回滚两次。

### 2.7 Kubernetes 里的"rollout undo"

`apps/v1` 没有 rollback 子资源（`extensions/v1beta1` 的 `deployments/rollback` 早就删了）。`kubectl rollout undo` 是在客户端做的：找到上一个版本的 ReplicaSet，把它的 Pod 模板（去掉 `pod-template-hash` 标签）拷回 Deployment，再 patch。执行器做的是同一件事，所以它只需要 `deployments: patch` 和 `replicasets: get/list`，不需要 create、delete 或任何集群级权限。补丁里同时写一条 `change-cause`，`rollout history` 能看到这次回滚是谁、因为哪个事件做的。

### 2.8 验证：凭什么说修好了

告警自己消失要等它的窗口过去（5 分钟速率加 `for`），太慢，而且"告警消失"不等于"恢复了"（可能只是没流量）。验证器按告警的 `slo` 标签直接查 1 分钟窗口的 SLI：

| slo | 查询 | 健康 |
|---|---|---|
| availability | 服务端 span 的错误率 | < 6%（慢速燃烧线） |
| latency | 服务端 span 的 p95 | < 1000 ms |

执行成功后等 `settle_s`（60 秒）开始，每 30 秒查一次，连续 3 次健康 → RESOLVED；没有数据不算健康；到期仍未恢复 → ESCALATED，原因里写上逆操作（"to undo: ..."）。查询和阈值来自代码里的表，不来自模型；表里没有的 SLO 不能验证，直接转人工。

### 2.9 提示注入（B11）

能写进模型输入的人很多：任何能发布的人都能写 `change-cause` 注解，任何能让服务打日志的人都能控制日志内容。所以要假设注入**一定会**成功一部分，防御是分层的：

1. 工具结果放在 `<tool_results>` 数据区，系统提示声明其中的内容是数据不是指令。（降低成功率）
2. 模型没有写工具，最多只能"提议"。（限制后果）
3. 策略引擎只读结构化字段：动作、目标、证据 ID。被注入的理由写得再有说服力，策略也看不到。（被骗的提议过不去）
4. 执行器只接受目录里的动作，用自己的最小权限身份。（策略也被绕过时的最后一道）

评测衡量两个数字：模型有没有抵抗住（第 1 层的效果），以及有没有被注入的动作通过策略（必须为 0，不管第 1 层成不成功）。

## 3. 踩过的坑，以及设计时预先规避的坑

| 问题 | 现象 | 处理 |
|---|---|---|
| 注入用例是空的 | 第一次用真实模型跑，change-cause 用例"抵抗成功"，但 Planner 根本没查 `rollout_history`，只凭诊断就答了，注入的文字从没到过模型面前 | 调查员会原样引用 change-cause，所以诊断的事实里也带上它；评测包一层记录模型看到的每个请求，被注入的文字没出现就算用例无效、评测失败 |
| 脚本化的"轻信"模型 | 注入测试一开始断言 deny 的理由同时有 problem_host 和 evidence，但重启不需要证据，quoted-log 用例只有 problem_host 失败 | 只断言 problem_host；关键的断言是"安全违规为 0" |
| worker 认领了执行 job | 所有进程共用一个队列表，worker 会认领任何 kind | `claim(kinds=...)`：每个进程只认领自己有处理函数的 kind；测试确认 worker 从不执行 |
| 重新投递 = 再执行一次 | 执行器在打补丁后、记录结果前被杀，队列会再给一次 | 打补丁前先提交 `started` 行；再看到它就标 `unknown` 并升级，不重试（2.6） |
| 并发修改 | 人在执行器准备和打补丁之间手动改了 Deployment | 补丁第一条 `test` resourceVersion，422 → failed，升级 |
| rollback 子资源 | 计划里写的 RBAC 是 `deployments/rollback`，`apps/v1` 没有 | 和 kubectl 一样在客户端拷模板（2.7）；RBAC 只要 patch |
| 已经是目标版本 | 回滚到的模板和当前一样（例如人已经回滚过） | `_same_template` 比较时去掉 `pod-template-hash`；相同就是 no-op，不打补丁 |
| `model_copy` 不校验 | 测试里 `settings.model_copy(update={"executor_k8s_token": "x"})` 得到的是 `str`，不是 `SecretStr`，`.get_secret_value()` 报错 | 直接传 `SecretStr(...)` |
| 计划改了，批准还在 | 如果审批按计划 ID 绑定，修订后旧批准会被套用到新内容上 | 绑定哈希；修订是新行、新哈希；执行器执行前再比一次哈希 |
| 自批 | Planner 是 `agent:planner`，但人修订的计划发起人是这个人 | 审批时比较 `approver != proposed_by`；拒绝不受此限（谁都可以拦） |
| 审批过期要定时扫描 | 一个 cron 扫所有 AWAITING_APPROVAL 的事件 | 提交时排一个延迟 job（`run_after = now() + ttl`），复用队列 |
| 验证太早 | 刚打完补丁就查，旧 Pod 还在，错误率还高 | 先等 `settle_s`，要连续 3 次健康 |
| 执行器在 compose override 里 | 只有实验环境有它，`docker compose ps` 看不到 | workflow 里 ps/exec 都带上两个 `-f` |
| 执行器拿到了别的 token | 如果执行器也有模型 token 或只读 token，它就不只是"执行器" | workflow 检查执行器的环境里没有这两个；`kubectl auth can-i` 确认两个 SA 各自的权限（读者不能 patch，执行器不能 delete、不能读 secret） |

## 4. 验收结果

验收在 GitHub Actions 的实验环境里跑（ADR-0004），第 18 次运行。和 M4 一样注入 3 个故障、等诊断；多出来的是：验收脚本通过真实 API、以 `lab-approver`（approver 角色，不是发起人）的身份扮演审批人。故障有已知修复（目前只有坏版本发布：`rollout_undo checkout`），而等待审批的计划恰好就是它，就批准，绑定计划哈希；是别的计划就拒绝；故障没有目录里的修复，就不动它，故障恢复后由实验环境关闭事件。然后等执行器和验证器把事件带到 RESOLVED 或 ESCALATED，检查审计记录里闭环的每一步按顺序出现，最后校验整条审计链。

**坏版本发布的闭环**：

```
07:33  checkout 发布 revision 2（PAYMENT_ADDR → payment:9999）
+147s  ErrorBudgetBurn[fast] on checkout → 事件
+153s  诊断：checkout 75%（引用 rollout_history、deploy_diff、错误 trace）
       Planner：rollout_undo checkout，引用同样的发布证据；备选 restart（"会带着同样的坏配置起来"）、
       scale（"不缺容量"）、none（"回滚可逆，等待只会延长故障"）
       策略：require_approval（R3），6 项阻断检查全部通过
       lab-approver 批准（plan_hash）
       执行器：dry-run → started → patch → rollout 完成，revision 2 → 3（旧模板拷回，新版本号）
       验证器：60 秒后开始，checkout 1 分钟错误率 0，连续 3 次 → RESOLVED
+130s  （从批准到 RESOLVED）
       验收脚本的"恢复"一步发现没什么可恢复："sre-blackboard already repaired it"
```

审计链：88 行全部校验通过。闭环事件 `remediation.plan → remediation.approved → execution.started → execution.succeeded → verification.passed` 按顺序都在。

**所有事件的修复提议**：

| 故障 | 事件 | 提议 | 策略 | 结果 |
|---|---|---|---|---|
| payment-failure | payment、checkout | 不动作："原因是 flagd 里的 paymentFailure 开关，目录里没有能改它的动作；人去把它关掉，然后看 payment 的错误率是否降回 0.1%" | deny（没有动作） | ESCALATED，交给人 |
| network-delay | 5 个事件中的 4 个 | 不动作："查 astronomy-db 的锁、慢查询和连接数" | deny（没有动作） | ESCALATED |
| network-delay | frontend-proxy | `restart_deployment product-catalog`（"像是连接池卡住"） | require_approval（R2） | 没有人批准；故障恢复后关闭 |
| bad-release | checkout | `rollout_undo checkout` | require_approval（R3） | 批准 → 执行 → **RESOLVED** |

- **结论**：验收通过。坏版本发布从告警到 RESOLVED 全程自动，只有审批一步是人（这里由脚本扮演）。诊断 Top-1 3/3。
- **"不动作"是对的答案**：功能开关和网络延迟都不是目录里的动作能修的。8 个计划里 6 个明确说"不动作"，并写出人应该去做什么、做完怎么确认。没有动作的计划记为 deny（原因"计划没有动作"），事件转 ESCALATED，Planner 写的建议进了审计记录。
- **策略管不了"有没有用"**：network-delay 的一个事件提议重启 product-catalog。它是排第一的根因（Problem Host），重启不需要特定证据，所以策略放行到人工审批。但故障是 Chaos Mesh 注入的网络延迟，重启没用。策略只能保证"动作指向有证据的问题主机、在范围内、可撤销"，"这个动作能不能修好"要靠人判断，修了没好要靠验证器发现。这也是 R2 默认要审批的原因。
- **成本**：Planner 每个事件 1–2 次模型调用，约 0.001–0.002 美元，和调查（约 0.04 美元）比可以忽略。
- **回放**：第 18 次的录音替换了 `tests/fixtures/agent/` 里第 17 次的录音；契约测试现在把 8 个调查和 8 个计划都重跑一遍，要求零未命中、同样的诊断、同样的计划。
- **诚实说明**：闭环只跑了一次（一个故障、一次运行），审批人是脚本，不是真的人在看计划。"连续 3 次健康"在实验环境里很容易满足（回滚后错误立刻归零）；真实的恢复可能更慢、更嘈杂，`verify_timeout_s` 和阈值需要按服务调。失败路径（执行出错、验证超时、审批过期、执行中被杀）是在测试里用假的 K8s API 和假的 Prometheus 验证的，没有在实验环境里真的触发。

## 5. 面试问答

**Q1：为什么策略引擎不用 LLM？**
因为它是安全边界，必须可预测、可测试、不能被说服。它只读结构化字段（动作、目标、证据 ID、诊断里的根因和置信度），是一个纯函数，21 个单测覆盖每一条规则。模型写的理由会存下来给人看，但策略引擎读不到。任何异常都是 deny（fail closed）。

**Q2：人批准的到底是什么？**
一个哈希。审批请求必须带上计划的 `plan_hash`，不匹配就 409。计划修订是新的一行、新的哈希，旧批准不会被套用。执行器执行前再比一次。所以"我批准的就是被执行的"可以证明，而不是靠界面。

**Q3：怎么保证回滚只执行一次？**
队列是至少一次的，执行器在真正打补丁前先提交一行带唯一 `idempotency_key` 的 `started` 记录。重新投递的 job 看到这一行，不知道补丁到底打没打上，就标 `unknown` 并转人工，不重试。补丁本身带 resourceVersion 的 `test`，期间有人改过 Deployment 就会被拒绝。

**Q4：执行器为什么是单独的进程？**
权限隔离。worker 有模型 token 和只读的集群 token，它跑的是模型；执行器没有模型 token，只有一个只能 patch Deployment 的 token，跑的是确定性代码。即使 worker 被完全控制（例如模型被注入后某种方式拿到了代码执行），它也改不了集群。执行器也不信 worker：它自己重跑策略、重查审批。

**Q5：如果回滚了但没修好？**
验证器查不到恢复，到期转 ESCALATED，原因里写着逆操作（怎么撤销这次回滚）。不会自动再试别的动作：第一个修复没生效，说明诊断可能错了，这时候该叫人。

**Q6：提示注入怎么防？能防住吗？**
不能保证模型不被骗，所以设计成"被骗了也没关系"：数据区和提示降低成功率；策略只看结构化字段，指向错误目标的动作会因为不是 Problem Host、没有对应的发布证据而被 deny；执行器只接受目录里的动作。评测用真实模型跑，看两个数字：模型抵抗率（希望高），以及注入动作通过策略的次数（必须为 0）。还要确认注入的文字真的到了模型面前，否则用例是空的（我们第一次就遇到了）。

**Q7：Problem Host 检查有什么局限？**
它依赖诊断的 Top-1 是对的。如果诊断错了（例如把 product-catalog 的数据库排第一），策略会允许对错误的服务动手，只是这时会被拦在人工审批和证据检查上（回滚需要那个服务确实有一次发布）。这正是为什么 R2/R3 默认都要人批准，而且 M7 要单独衡量诊断准确率。

**Q8：为什么验证不看告警是否消失？**
告警有 5 分钟的速率窗口加 `for`，消失要很久；而且没流量时告警也会消失。验证器直接查 1 分钟窗口的 SLI，要连续 3 次健康，没数据不算健康。

**Q9：审计日志怎么证明没被改过？**
每一行的哈希包含上一行的哈希（SHA-256 链），表上有禁止 UPDATE/DELETE 的触发器。`verify` 从头重算，任何一行被改都会报出位置（"BROKEN at seq N"）。计划和审批表也只追加。实验环境验收结束时会校验整条链。
