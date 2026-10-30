# ADR-0011：系统自身的可观测性——一个事件一棵 trace（上下文经 Postgres 传递）、Prometheus 指标、只用于告警给人的 SLO 规则

- 状态：已接受
- 日期：2026-10-30
- 相关里程碑：M8

## 背景

到 M7 为止，系统能诊断别人的故障，却说不清自己的状况：

- **一次调查是怎么花掉时间和 token 的**，只能去 `llm_calls`、`tool_calls`、`agent_runs` 三张表里按事件 id 拼。M7 实测默认变体诊断 p50 是 166 s，但慢在哪个 Agent、哪一步、哪个工具，没有现成的视图。
- **队列积压、死信、worker 全挂**，都只能登进数据库查。worker 挂了以后，它自己也报不了"我挂了"。
- **订阅额度**是这个项目最紧的资源，却没有按 Agent 统计的 token 曲线，也没有"一小时花太多了"的告警。

约束如下：

- **一个事件横跨多个进程**：API 收告警、开事件；worker 分诊、调查、出方案、核验；执行器改集群。进程之间唯一的通道是 Postgres 里的任务队列（ADR-0003），没有 HTTP 调用可以带 trace 头。
- **API 是多进程的**（uvicorn `--workers 2`），worker 和执行器是单进程。
- **实验环境里已经有 Jaeger 和 Prometheus**，它们属于 OpenTelemetry Demo。其中的 Prometheus 用 collector 生成的 span 指标算 SLO 告警，告警会发给本系统开事件。
- **CI 里不能调用模型**。

## 决定

**一、trace：一个事件一棵树，上下文经队列传递**

1. **事件的根 span 由 API 在开事件时创建**。它是一条新的 trace（不是 webhook 请求那条），通过 span link 指回请求。根 span 立即结束，它的 traceparent（W3C 格式）存进 `incidents.trace_context`（迁移 0006）。
2. **入队时带上上下文**。`jobs.enqueue` 写入 `COALESCE(事件的 trace_context, 当前 span)`：凡是属于某个事件的任务，父 span 一律是事件的根。审批也遵守这条规则：它在 HTTP 请求里入队 `execute`，但执行任务仍然挂在事件的树上，而不是挂在审批请求的 trace 上。
3. **认领任务的进程接着这条 trace 往下记**。`job <kind>` 是 CONSUMER span，属性包括尝试次数、优先级、排队等待时长和结局。回放评测（`app/eval/replay.py`）走同一段代码，所以 CI 可以不调模型就产出完整的树。
4. **Agent 层遵循 OpenTelemetry GenAI 语义约定**（目前是 development 状态）：
   - `invoke_agent <角色>`：模型、提示词版本、步数、工具调用次数、改写次数、被丢弃的发现；
   - `chat <模型>`：`gen_ai.usage.input_tokens` 按约定包含缓存命中的 token，缓存读写另记在 `cache_read` / `cache_creation`；
   - `execute_tool <工具>`：参数、证据 id、错误码。
5. **不把提示词和模型回答放到 span 上**。它们已经存在 `llm_calls` 里，每个 `chat` span 带 `sre.llm.request_hash`，可以据此关联。工具参数（PromQL、LogQL）会放到 span 上：排查时最先要看的就是 Agent 问了什么，而且参数里没有密钥。
6. **其余也在同一棵树上**：
   - `policy.evaluate`：裁决、风险、没通过的检查；
   - `remediation.action <动作> <对象>`：执行器的每个动作；
   - 状态迁移和核验采样：作为 span event 记录。
7. **HTTP 请求的 span 用 FastAPI 0.143 自带的**（遵循 HTTP 语义约定）。关掉了它的 operation span 和 OTel 指标；`/api/v1/stream`（长连接）、探针和 `/metrics` 不打 span。
8. **日志带 `trace=<id>`**，日志和 trace 可以互相找到。
9. **没有配置 `SRE_OTEL_ENDPOINT` 时什么都不导出**：tracer 是 OpenTelemetry 的空实现，事件上也不存上下文。

**二、指标：Prometheus，标签有界**

10. **在事情发生的地方计数**：
    - 状态停留时长，以及开事件到首次诊断的时长；
    - 队列等待（从可运行到被认领）、任务结局和耗时；
    - 模型调用：按 Agent 角色和模型统计次数、token、成本、时延；
    - 工具：按工具统计错误码和时延；
    - 策略裁决（提交时和执行前各一次）、审批等待、执行、核验；
    - HTTP 请求：按路由模板统计。
11. **数据库里"此刻"的状态在 API 被抓取时现查**：
    - 队列深度、最老的可运行任务等了多久；
    - 未结束的事件，以及最老的未诊断事件；
    - 最老的待审批方案。

    不由 worker 上报：worker 全挂的时候，恰恰最需要这些数字。每个优先级都输出（没有就是 0），告警看到的是 0，而不是"序列不存在"。
12. **事务里的观测等提交以后才记**（`metrics.after_commit`，挂在引擎的 commit/rollback 事件上）。因版本冲突或租约丢失而回滚的状态迁移不计数。没有挂钩子的引擎会立即执行，不会在连接上越攒越多。
13. **标签只用有界的集合**：任务种类、状态、优先级、Agent 角色、工具名、模型、错误码。
    - 不用租户、事件、服务作标签，这些在 trace 里。
    - 模型编出来的工具名统一记为 `unknown`。
    - 直方图带 exemplar（trace id），Grafana 上的一个慢点可以直接跳到对应的 trace。
14. **多进程**：
    - API 设置 `PROMETHEUS_MULTIPROC_DIR`，抓取时把各进程的值加总。这个环境变量只在 uvicorn 的启动命令里设，因为执行器也用 API 镜像，它是单进程，要用自己的 registry。
    - worker 和执行器在 `SRE_METRICS_PORT` 上各自暴露指标。端口被占用只记一条警告，不让进程起不来。

**三、仪表盘和告警**

15. **仪表盘是代码**：`scripts/dashboard.py` 生成 `deploy/observability/grafana/dashboards/sre-blackboard.json`。测试检查三件事：
    - JSON 和脚本的输出一致；
    - 面板和告警只用 `app/obs` 定义过的指标；
    - 每个指标至少出现在一个面板上。
16. **SLO**：
    - **目标**：90% 的 P0 事件在 2 分钟内得到首次诊断。
    - **两条告警**：
      - `P0DiagnosisLate`（page）：此刻有 P0 超过 2 分钟还没有诊断，这时人手去看还来得及；
      - `P0DiagnosisObjectiveMissed`（ticket）：最近一小时的达标率低于 90%。
    - **当前达不到**：M7 实测默认变体 p50 166 s、p95 222 s，单 Agent 变体 p50 49 s、p95 136 s。这个目标暴露的是差距，不是现状。
17. **其他告警**：积压、死信、worker 全挂、读不到队列状态、模型报错率、工具后端故障、token 消耗、审批久等。
    - 工具告警只算 `timeout|unavailable|backend_error|forbidden`。`invalid_args`、`bad_query` 是 Agent 自己问错了，它会被告知并改正，不算后端故障。
    - 全部规则都有 promtool 单元测试。
18. **这些告警不发给本系统**。理由有三：
    - 系统挂了、卡住了或者额度用完了，就没法调查自己的故障；
    - 一个"调查器出问题"的事件，会由调查器自己去调查，花模型额度调查自己；
    - 应该发到人的渠道。compose 里的 Prometheus 只负责评估和展示。

**四、部署**

19. **`docker compose --profile observability`** 增加 Prometheus（DNS 服务发现，每个 worker 副本一个目标）、Grafana（自动加载数据源和仪表盘）和 Jaeger。
    - Jaeger 固定在 2.20：2.21 去掉了 `/api/services`、`/api/traces`，而 Grafana 13.2 的 Jaeger 数据源还在调用这两个接口。
20. **实验环境里，三个进程把 trace 直接发给 Demo 的 Jaeger**：新开一个 NodePort（`deploy/kind/manifests/jaeger-otlp.yaml`），API 也加入 kind 的网络。
    - 刻意不经过 Demo 的 collector。collector 会把 span 变成 span 指标，实验环境的 SLO 告警正是用这些指标算的。本系统的报错 span 一旦进去，就可能触发告警，告警又发给本系统，变成自己调查自己。

**五、验收（不调用模型）**

21. **CI 的 e2e 任务**：启动带 observability profile 的整套 compose，跑 M1 的压测，再运行 `scripts/obs_check.py`，检查：
    - 指标和压测对得上（告警数、事件数、分诊数，队列清空）；
    - 每条规则都已加载且健康，每条规则和面板查询都能在真实的 Prometheus 上求值；
    - Grafana 的仪表盘和两个数据源都正常；
    - Jaeger 里有一条 trace，根 span 来自 API、`job triage` 来自 worker，说明上下文确实经 Postgres 跨了进程；
    - 回放一段录好的调查，导出到同一个 Jaeger，树的形状对：3 个 job、Agent、模型调用（带 GenAI 属性）、工具调用、策略。

## 后果

**好处**

- 慢在哪里可以直接看出来。M7 那个 166 s 的 p50，现在能拆到每个 Agent 的每一步：模型调用多久，工具调用多久，改写了几次。
- 额度有了曲线和告警。按 Agent、按 token 类型统计，能看出缓存读取占了多少。
- 即使 worker 全挂，API 仍然报告队列在积压。
- 整套验收在 CI 里跑，不花额度。

**代价**

- **根 span 的时长是 0**。树的总时长要看子 span，Jaeger 会显示子 span 比父 span 结束得晚。这是跨进程、长时间流程的正常形态。
- **多一个迁移、一列上下文**。只开了 trace 的进程才会写入这一列。
- **API 多进程用的是文件**。每次启动都清空目录；`livesum` 类的 gauge 遇到进程退出，可能短暂偏大，不过 API 不更新这类 gauge。
- **多进程模式下没有 exemplar**（prometheus_client 不支持）。API 的直方图没有 exemplar，worker 的有。
- **GenAI 语义约定还在 development 状态**，属性名可能变化。用到的属性名集中在 `app/obs/tracing.py` 里。

**还没做**

- **实验环境里真实模型的 trace 还没有跑过**。实验环境会调用模型，需要用户确认后才跑。CI 里用的是回放，回放的时长是回放的时长，不是真实耗时。
- **告警还没有接到人的渠道**（Alertmanager 或聊天 webhook）。
- **trace 没有采样**：一个事件一棵树，几十到一百多个 span，量不大。事件量大了以后，需要尾部采样（保留慢的和失败的）。
- **控制台还不能跳到 trace**：控制台的事件页还没有"打开 trace"的链接，API 也还没把 trace id 返回给前端。
