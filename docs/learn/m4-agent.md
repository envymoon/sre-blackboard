# M4 学习笔记：Agent 核心——手写循环、可插拔 LLM 后端、黑板与引用校验

> 对应里程碑：M4 · 相关决策：[ADR-0006](../adr/0006-agent-loop-llm-backends-blackboard.md)
> 这一阶段第一次让真实的 LLM 读真实的数据：告警进来，Coordinator 写计划，3 个 Investigator 并行查数据，Synthesizer 写出带证据链的根因报告。每个结论都能点开看到原始数据，整个过程可以在没有模型、没有集群的地方逐字节重放。

## 1. 这一阶段做了什么

| 类别 | 内容 |
|---|---|
| LLM 后端 | `app/llm/`：一个接口，三种实现（`claude -p` 订阅、Anthropic API、回放）；重试、限流识别、每进程并发上限、按角色选模型 |
| 探针 | `scripts/llm_probe.py`（`make llm-smoke` / `make llm-probe`）：实测 CLI 的输出字段、错误形态、工具隔离 |
| Agent 循环 | `app/agents/loop.py`：渲染 → 一步 → 校验 → 执行工具 → 追加结果；最后一步只能答；每步之前检查预算 |
| 角色 | Coordinator（计划）→ 3 个 Investigator 并行（metrics、logs_traces、changes）→ Synthesizer（诊断）；prompt 放在 `app/agents/prompts/`，带版本号 |
| 引用校验 | `app/agents/citations.py`：ID 存在且属于本事件、引用者看过、类型匹配；不合格退回一次，再不合格就丢弃 |
| 黑板与检查点 | `agent_runs`（每个 Agent 一行）+ `blackboard`（只追加）+ `llm_calls`（账本）；同一事务写入；job 重跑时跳过已完成的 Agent |
| 状态机 | triage 排队 investigate；TRIAGED → INVESTIGATING → DIAGNOSED，或 ESCALATED（预算用完、没有站得住的根因、模型一直不可用） |
| API | `GET /incidents/{id}/diagnosis`、`/report`（Markdown）、`GET /evidence/{id}`，都按租户隔离 |
| 录制回放 | `app/agents/recording.py`：从数据库导出一次调查，装进空库再跑一遍，结果相同 |
| 实验环境 | worker 镜像带 Claude CLI；compose 的 worker 接入 kind 的 Docker 网络；验收在每个故障进行中等诊断并打分 |

## 2. 背景知识

### 2.1 Agent 循环到底是什么

去掉所有包装，一个工具型 Agent 就是这样一个循环：

```
transcript = [任务]
重复：
    step = 模型(系统提示, transcript)        # 要么"我要查 X、Y"，要么"答案是 Z"
    如果是查询：执行查询，把结果追加到 transcript
    如果是答案：校验；不合格就把问题追加进去，再来一轮
```

关键在于**谁执行**。模型只输出结构化的"提议"，执行永远在我们的代码里。所以可以在执行前做任何检查：这个 Agent 有没有这个工具的权限、预算还够不够、这是不是最后一步。这也是安全边界：模型说"去重启 Pod"，它手里根本没有重启工具。

框架（LangGraph 等）把这个循环包装成图和状态对象。我们没有用，因为需要精确控制三件事，而框架会把它们藏起来：每一步的检查点、job 的 fence、按请求哈希回放。循环本身只有一百多行。

### 2.2 `claude -p` 当作"单步推理引擎"

Claude Code 的无头模式本来是一个完整的编码 Agent：有自己的工具、自己的循环、自己的系统提示。我们只要它的"一步推理"，所以要把其余的全关掉：

| 参数 | 作用 |
|---|---|
| `--system-prompt` | 替换掉自带的编码助手提示，换成我们的角色提示 |
| `--tools ""` | 一个内置工具都没有（Bash、Read、Edit……） |
| `--strict-mcp-config`、`--setting-sources ""` | 不加载任何 MCP server 和本机设置 |
| `--json-schema` | 输出必须符合这份 schema，结果在 `structured_output` 字段 |
| `--output-format json` | 一次性返回 JSON，带 `usage`、`total_cost_usd`、`is_error` |
| `--no-session-persistence` | 不存会话；每步无状态，transcript 由我们渲染 |
| 空的工作目录 | 不让它读到任何 `CLAUDE.md` |

`--json-schema` 的实现方式值得知道：CLI 给模型加了一个叫 `StructuredOutput` 的工具，模型通过调用它来交答案。所以一次正常的 `-p` 调用 `num_turns` 是 2（调用工具 + 收到确认）。

### 2.3 结构化输出：一步的形状

每一步的 schema（CLI 后端）：

```json
{"reasoning": "两三句话：看到了什么、下一步做什么",
 "actions": [{"tool": "prom_query", "args": {"query": "..."}}],   // 至多 3 个，并行执行
 "final": { ... }}                                                 // 只在结束时给
```

`actions` 的每一项是一个 `anyOf` 分支：`tool` 是常量，`args` 是那个工具自己的参数 schema。这样模型在填 `args` 时就有精确的字段约束。参数是否合法最终仍由工具注册表校验（M3），错误会变成模型能看懂的结果返回给它。

API 后端不需要这层包装：工具是原生的 `tool_use`，最终答案是一个叫 `submit_final` 的工具，`tool_choice: any` 保证模型每步必须调用某个工具。

### 2.4 为什么要"确定性的引用校验"

LLM 会编造。在诊断场景里，最危险的编造是"引用了一条不存在的证据"：结论看起来有据可查，其实没有。防这个不需要另一个模型，三条规则就能确定性地检查：

1. **存在且属于本事件**：查 `evidence` 表，`incident_id` 必须匹配。
2. **看过**：Investigator 只能引用自己查询返回的 ID，不能引用别的 Agent 的，也不能"记得"一个 ID。
3. **类型匹配**：说"日志里出现了 X"，就至少要引用一次日志查询。

不合格的答案退回一次，附上逐条问题（"findings[1]: ev_00000000deadbeef is not an evidence id of this incident"）。模型通常能改对；改不对的条目丢弃，并记下丢了几条。这比"整份答案作废"好：一个错误的引用不应该连累其余正确的结论。

### 2.5 黑板模式与检查点

多个 Agent 共享状态，常见做法是让它们互相对话。对话的问题是：难以检查点（状态散在消息里），难以回放（顺序不确定），容易互相带偏。黑板模式是：**每个 Agent 读公共的黑板，只写自己的行**。

- `blackboard` 只追加，每行有作者；并行的 Agent 不可能覆盖彼此。
- 一个 Agent 的全部输出和"它完成了"（`agent_runs.status = done`）在同一个事务里提交。所以不存在"写了一半"的状态。
- 这个事务走 job 的 fence：如果这个 worker 已经丢了租约（另一个 worker 接手了），提交会被拒绝。

job 重跑时，读 `agent_runs`：已完成的 Agent 直接从黑板取结果，不再调用模型。这就是"在 INVESTIGATING 中途杀掉 worker，接手的 worker 不会重复调用已完成的 Investigator"。

### 2.6 预算与成本

两种"花费"：

- **token**：每一步都要重发整个 transcript，所以总量随步数近似平方增长。每一步之前检查事件已用的 token，超过上限就 ESCALATED。
- **等价成本**：CLI 的 JSON 里有 `total_cost_usd`（按 API 价格计算的等价成本）；API 只返回 token，我们不维护价格表，所以那时成本为空。

一个 CLI 特有的成本：transcript 通过标准输入作为一整块文本传入，每一步都和上一步不同，所以每一步都是"写缓存"（`cache_creation_input_tokens`），读不到上一步的缓存。API 后端在 transcript 末尾设缓存断点，下一步只为新增的部分付全价。

### 2.7 让 LLM 系统可回放

M3 让工具可回放，M4 把模型也接上：

- 模型的每一步按 `(agent, prompt 版本, 请求哈希)` 存进 `llm_calls`。请求哈希覆盖系统提示、工具定义、整个 transcript。
- transcript 里的工具结果带着由内容派生的证据 ID（ADR-0005），所以只要工具返回同样的原始数据，transcript 就逐字节相同，请求哈希就相同，就能取到同一个回答。
- 要可重复，渲染给模型的内容就不能依赖时钟或持续变化的值。M4 修掉了三处：告警计数（不断增长）、最新通知的标注（可能带新的数值）、"相关事件"（改成按创建和关闭时间计算）。
- 并行的 Investigator 可能几秒内发出同一个查询，拿到不同的结果；同一故障的几个事件也会问同样的问题。工具录音按事件、再按提问的 Agent 分别回放，回放的顺序就不重要了。

## 3. 踩过的坑，以及设计时预先规避的坑

| 问题 | 现象 | 处理 |
|---|---|---|
| 数据源写在系统提示里 | 探针中 Haiku 先把 `prom_query` 当原生工具调用，得到 `No such tool available`，第二轮才用 `actions`；`num_turns` 3，提示 5.2k token，4.7 秒 | 数据源只写进输出 schema（`actions` 的 `anyOf` 分支），系统提示里不提；改后 `num_turns` 2，提示 2.6k，3.2 秒 |
| 只在结尾加一句"请用 StructuredOutput" | 没用，模型照样先调一次不存在的工具 | 同上：问题出在模型"看到"的工具定义，不在指令 |
| 未知模型 | 退出码 1，`is_error: true`，`api_error_status: 404`，`subtype` 仍是 `success` | 先看退出码和 `is_error`，再按 `api_error_status` 分类；不信 `subtype` |
| 无法满足的 schema | 退出码 0，没有 `structured_output`，`result` 是一段道歉 | 没有 `structured_output` 就是 `invalid_output`，可重试 |
| 空的一步 | 实测中模型偶尔返回 `actions` 为空且没有 `final` | 当作 `invalid_output` 重问；重试后仍然如此只让这个 Agent 失败，不让整个 job 失败 |
| 嵌套 Claude Code | 开发容器里设了 `CLAUDECODE`，子进程会以为自己在另一个会话里 | 启动子进程时去掉这个变量；在空目录里运行 |
| 订阅用量窗口 | 限流可能要几小时才恢复 | 等待时间超过 2 分钟就放弃，交给队列稍后重试整个 job；已完成的 Agent 保留 |
| `asyncio.gather` | 一个 Investigator 预算用完抛异常，其余的还在后台继续调用模型 | 改用 `TaskGroup`，异常时取消其余任务 |
| 回放漂移 | 告警计数、标注、相关事件会随时间变化，导致请求哈希不同 | 只用稳定的存储值渲染（见 2.7） |
| 并行的相同查询 | 两个 Investigator 同一查询拿到不同时刻的数据，回放顺序不同就会对不上 | 工具录音按 Agent 分别回放 |
| 事件不关闭 | 实验环境里同一服务同一告警会合并进上一个故障的事件，不会触发新的调查 | 验收脚本在故障恢复后把事件转 ESCALATED（"lab: fault recovered"）；真正的关闭是 M5 的事 |
| worker 镜像 | Claude CLI 是一个约 250 MB 的原生二进制，通过 npm 分发 | 多阶段构建：node 镜像里 npm 安装，只拷贝二进制；只有 worker 镜像带它 |
| CI 负载测试 | 200 个事件都会排队调查，CI 里没有模型 | `SRE_AUTO_INVESTIGATE=false`：只做分诊 |
| CLI 版本太旧 | 2.1.292 不认识 `claude-haiku-5-5`：调用照样成功，但 stderr 先打一行 `unrecognized_model` 警告，成本按别的模型算（高约 15 倍）。检查脚本把 `2>&1` 合进 JSON 再解析，好用的 token 看起来像坏了 | 升级到 2.1.300；所有检查只从 stdout 读答案，stderr 只在失败时显示 |
| token 放错地方 | 仓库 secret 放在 Agents / Codespaces / Dependabot 下，Actions 读到的是空字符串，每次调查都以 "Not logged in" 升级 | 加一个一分钟的 `Model check` workflow，先确认 token 能用，再花一个半小时跑实验环境 |
| `too_large` 没进录音 | 导出时把它当成注册表自己产生的错误跳过了，可它是在后端回答之后才判定的；回放时这次查询没有答案，后面整条 transcript 都对不上 | 只有 `invalid_args`、`unknown_tool` 不录；`too_large` 连同原话录下来 |
| 同一故障的多个事件 | 网络延迟一次开了 5 个事件，同名 Agent 在几分钟内问了同样的问题；回放其中一个时拿到了别的事件的答案 | 工具录音带上事件 ID，每个事件只用自己的答案回放 |
| 把"等待"当成"原因" | 网络延迟注入在 product-catalog 上，它访问数据库的客户端 span 也一起变慢；一个事件据此把数据库排第一，尽管数据库 CPU 几乎为零 | Synthesizer 只凭依赖自己一侧的证据怪它；调用方的客户端 span 只说明它在等什么 |

## 4. 验收结果

（实验环境运行后补充。）

本地用真实的 Haiku 跑了一次完整调查：工具回放 M3 录下的支付故障数据，模型提出的大多数查询不在录音里（36 次查询只有 2 次命中）。即使如此，它仍把 payment 排在第一（置信度 0.5），并明确写出哪些数据没拿到。19 次模型调用，约 15.8 万 token，等价成本约 0.03 美元。

## 5. 面试问答

**Q1：为什么不用 LangGraph 之类的框架？**
要的只是"渲染 → 一步 → 校验 → 执行"这个循环，一百多行。真正难的是循环之外的东西：每个 Agent 的检查点要和 job 的 fence 绑定，回放要按请求哈希逐字节对上，预算要在每一步之前检查。框架把状态放在它自己的对象里，这些都要绕着它做。手写的代价是没有现成的可视化；我们用数据库里的 `agent_runs`、`llm_calls`、`blackboard` 代替。

**Q2：`claude -p` 做后端有什么代价？**
每步起一个进程（约 1 秒、250 MB），所以并发要限制；transcript 是一整块文本，每步都要写缓存，不能增量缓存；订阅有用量窗口。好处是不需要 API 账单，而且后端可插拔：同一个接口换成 API 后端，就有原生工具调用和增量缓存。

**Q3：模型怎么"调用工具"，但又什么都执行不了？**
模型只输出一个结构化的提议（工具名 + 参数），执行在我们的代码里。CLI 后端甚至没有任何工具：数据源只是输出 schema 里的一个字段。harness 在执行前检查这个 Agent 有没有这个工具、预算够不够。M5 的修复动作也会走同样的路：模型提议，策略引擎和人审批，独立的执行器执行。

**Q4：怎么防止幻觉？**
结论必须引用证据 ID，ID 有确定性的三条检查：存在且属于本事件、引用者看过、类型匹配。不合格的退回一次再丢弃。证据本身是不可变的快照加哈希（M3），所以"引用"能被任何人复查。剩下的风险是"引用是真的，但推理是错的"，这要靠评测（M7）来衡量，而不是靠又一个模型。

**Q5：并行的 Agent 怎么共享状态又不互相覆盖？**
黑板只追加，每行属于写它的 Agent；一个 Agent 的输出和"完成"标记在同一个事务里。没有两个 Agent 写同一行，所以不需要锁，也不会覆盖。Synthesizer 在所有 Investigator 结束后读黑板。

**Q6：worker 在调查中途被杀会怎样？**
job 的租约过期，另一个 worker 接手。INVESTIGATING 是恢复点：已完成的 Agent 从黑板读结果，不再调用模型；被打断的 Agent 从头开始，它的工具调用复用已有的证据行。被杀的 worker 如果其实还活着（只是卡住了），它的写入会被 fence 拒绝。有一个测试真的 SIGKILL 一个 worker 进程来验证这件事。

**Q7：怎么测试一个 LLM 系统？**
三层：(1) 单元测试用脚本化的"模型"，但工具跑真实代码、读真实的实验环境录音，所以校验、检查点、引用规则都是真实运行的；(2) 一次真实运行导出成录音，回放必须得到同样的诊断；(3) 实验环境里用真实模型对真实故障打分（Top-1 准确率）。前两层在 CI 里跑，不花额度。

**Q8：一次调查花多少钱？怎么控制？**
约 20 次模型调用、十几万 token，Haiku 下约 0.03 美元等价成本。控制手段：工具结果压缩（M3，模型看到的约是原始数据的 1/230）；每个 Agent 最多 8 步，最后一步强制回答；每个事件有 token 上限，超了就交给人；模型按角色可配置，默认都用最便宜的。

**Q9：为什么根因说不清时要转人工，而不是给一个最可能的答案？**
On-call 拿到一个看似确定、其实没有证据的根因，比拿到"不知道"更糟：他会朝错误的方向修。所以诊断里的根因候选必须有站得住的引用；一个都没有时转 ESCALATED，报告里写清楚查了什么、缺什么。

**Q10：报告里为什么区分事实、推断和假设？**
因为它们的可靠程度不同，读的人的处理方式也不同：事实可以直接用，推断需要知道依据，假设需要验证（报告里给出验证方法）。这个区分由结构化的 `Diagnosis` 保证，Markdown 只是它上面的模板。
