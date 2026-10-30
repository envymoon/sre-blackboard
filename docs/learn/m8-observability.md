# M8 学习笔记：系统自身的可观测性——一个事件一棵 trace、Prometheus 指标、只告警给人的 SLO

> 对应里程碑：M8 · 相关决策：[ADR-0011](../adr/0011-system-observability.md)
>
> 这一阶段让系统能说清自己的状况：
>
> - 一次调查慢在哪个 Agent、哪一步、哪个工具；
> - 队列有没有积压、worker 是不是全挂了；
> - 订阅额度按什么速度在花。
>
> 全部验收都在 CI 里跑，不调用模型。

## 1. 这一阶段做了什么

| 部分 | 内容 | 位置 |
|---|---|---|
| trace | 事件的根 span 由 API 开出；上下文存在 `incidents.trace_context`，入队时复制到 `jobs.trace_context`，认领任务的进程接着记 | 迁移 0006、`app/obs/tracing.py`、`app/queue/jobs.py`、`app/ingest.py` |
| span | `job <kind>`、`invoke_agent`、`chat`、`execute_tool`（GenAI 语义约定）、`policy.evaluate`、`remediation.action`；状态迁移和核验采样作为事件 | `app/worker/runner.py`、`app/agents/loop.py`、`app/tools/registry.py`、`app/remediation/flow.py`、`app/executor/job.py` |
| HTTP | FastAPI 0.143 自带的 server span；指标中间件按路由模板计数 | `app/api/app.py`、`app/api/metrics.py` |
| 日志 | 每行带 `trace=<id>` | `TraceIdFilter` |
| 指标 | 事件、队列、任务、模型、Agent、工具、策略、审批、执行、核验、HTTP；事务里的观测等提交后才记 | `app/obs/metrics.py` |
| 数据库状态 | 队列深度、最老的可运行任务、未结束和未诊断的事件、待审批：API 被抓取时现查 | `app/obs/state.py` |
| 仪表盘 | 6 组 34 个面板，由脚本生成 | `scripts/dashboard.py` → `deploy/observability/grafana/` |
| 告警 | 11 条规则（2 条 SLO、4 条队列、4 条依赖和额度，1 条记录规则），promtool 单元测试 | `deploy/observability/rules/` |
| 部署 | compose 的 `observability` profile：Prometheus、Grafana、Jaeger；实验环境直接发给 Demo 的 Jaeger | `docker-compose.yml`、`deploy/kind/` |
| 验收 | e2e 任务里跑 `scripts/obs_check.py`：指标对账、查询求值、Grafana、跨进程 trace、回放出完整的调查树 | `.github/workflows/ci.yml` |

常用命令：

```bash
# 整套加上可观测性，trace 发到 compose 里的 Jaeger
echo "SRE_OTEL_ENDPOINT=http://jaeger:4318" >> .env
docker compose --profile observability up -d
# Grafana http://localhost:3000   Prometheus http://localhost:9091   Jaeger http://localhost:16686

uv run python -m scripts.dashboard     # 改了面板以后重新生成 JSON
make rules-test                        # 两套告警规则的 promtool 单元测试
uv run python -m scripts.obs_check --replay <空库的 URL>   # 和 CI 一样的验收
```

## 2. 背景知识

### 2.1 trace、指标、日志各回答什么问题

- **指标**回答"有没有问题、多严重"。数字便宜、能聚合、能告警，但只能按有限的标签切分。
- **trace**回答"这一次慢在哪里"。保留每一次请求的因果结构，但量大，不适合直接告警。
- **日志**回答"那一刻具体发生了什么"。最细，但要靠 trace id 才能和别的信号串起来。

三者靠 id 连起来：

- 日志行上有 trace id；
- 直方图的 exemplar 上有 trace id；
- `chat` span 上有请求哈希，可以对回 `llm_calls` 表。

### 2.2 W3C traceparent 和上下文传递

一条 trace 由一个 128 位的 trace id 串起来，每个 span 有自己的 64 位 span id 和父 span id。跨进程时传递的是 `traceparent` 头，例如 `00-<trace id>-<span id>-01`。

HTTP 服务之间靠请求头传递。本系统的进程之间没有 HTTP，只有 Postgres 里的队列，所以把 traceparent 当作数据存进行里：

- **事件行**存根 span；
- **任务行**存它应该挂在哪个 span 下面。

这和消息队列的做法一样：Kafka 把它放在消息头里，这里放在任务行的一列里。

### 2.3 父子关系还是 link

两个 span 之间有两种关系：

- **父子**：表示"它是我的一部分"；
- **link**：表示"它和我有关，但不是我的一部分"。

本系统的选择：

- **一个 webhook 请求可以开出好几个事件**。每个事件是一条新的 trace，用 link 指回请求。如果做成请求的子 span，一批告警就会挤在一条 trace 里。
- **事件的所有任务都挂在根 span 下面**，包括审批请求入队的执行任务。审批请求自己是另一条 trace。这样"一个事件一棵树"在任何入队路径下都成立。
- **另一种做法是让任务挂在入队它的那个任务下面**，形成一条链：分诊 → 调查 → 方案 → 执行 → 核验，每一步嵌套一层。因果关系更细，但树会越来越深；而且审批这种从外部入队的任务，会把执行挂到审批请求的 trace 上。

### 2.4 GenAI 语义约定

OpenTelemetry 为大模型应用定义了一组属性名，目前是 development 状态：

- `gen_ai.operation.name`：`chat` / `invoke_agent` / `execute_tool`；
- `gen_ai.request.model`、`gen_ai.response.model`；
- `gen_ai.usage.input_tokens`、`output_tokens`、`cache_read.input_tokens`；
- `gen_ai.agent.name`、`gen_ai.conversation.id`、`gen_ai.tool.name` 等。

遵循约定的好处是工具能看懂。Jaeger 看到这些属性后，会多出一个 "GenAI View"。

有一个细节：约定里的 `input_tokens` 包含缓存命中的 token。本系统的 `Usage.input_tokens` 是未缓存的部分，所以 span 上写的是 `prompt_tokens`（三者之和），缓存读写另外单独记录。

### 2.5 Prometheus 基础

- **三种类型**：
  - counter：只增不减，用 `rate()` / `increase()` 看速度；
  - gauge：当前值；
  - histogram：分桶计数，用 `histogram_quantile()` 估分位数。
- **分桶要包含阈值**：P0 的目标是 120 s，所以 `STAGE_BUCKETS` 里一定有 120，"2 分钟内的比例"才能精确计算，而不是靠插值。测试里断言了这一点。
- **基数**：每一种标签取值组合是一条序列。
  - 用租户、事件 id、服务名作标签，序列数会随数据增长；
  - 模型编出来的工具名也是一样。本系统的标签只用有界集合，模型编出来的工具名统一记成 `unknown`。
- **`increase()` 会外推**。promtool 测试里，每 5 分钟一个点、一小时涨了 600 万，`increase(...[1h])` 算出来是 545.5 万：窗口两端各差半个采样间隔。告警阈值要留出这个余量。

### 2.6 多进程和"此刻"的数据

- **多进程**：prometheus_client 默认每个进程一份内存。uvicorn 多进程时，每次抓取只会碰到其中一个进程。解决办法是 multiprocess 模式：每个进程把值写进 `PROMETHEUS_MULTIPROC_DIR` 里的 mmap 文件，抓取时加总。
  - 代价：exemplar 不可用；gauge 要选聚合方式；目录要在每次启动时清空。
- **"此刻"的数据从数据库读**：队列深度、最老的未诊断事件，这类数据的唯一真相在数据库里。如果让每个 worker 上报，有两个问题：
  - 多个 worker 报的是同一个数，要去重；
  - worker 全挂的时候，这些数字也跟着消失，而这恰恰是最需要它们的时候。

  所以由 API 在被抓取时现查。多个 API 容器报的也是同一个数，查询时用 `max()`。

### 2.7 exemplar

直方图的某个桶里，附上最近一次落进这个桶的观测及其 trace id。Grafana 在曲线上画出这些点，点一下就能打开那条 trace。

两个前提：

- Prometheus 要开 `--enable-feature=exemplar-storage`；
- 抓取要用 OpenMetrics 格式，`prometheus_client` 的 HTTP 服务会按 Accept 头自动协商。

### 2.8 SLO 告警："此刻"和"达标率"

同一个目标，分成两条告警：

- **`P0DiagnosisLate`（page）**："此刻有一个 P0 超过 2 分钟还没有诊断"。针对单个事件，触发时人手接管还来得及。
- **`P0DiagnosisObjectiveMissed`（ticket）**："最近一小时达标率低于 90%"。针对系统的整体表现，需要改进，但不用半夜叫醒人。

M2 给实验环境写的是多窗口燃烧率告警，因为那里是高流量的请求 SLI。事件是低频的：一小时可能只有几个 P0，燃烧率很不稳定。所以这里一条盯"此刻"，一条盯"整体"。

### 2.9 监控系统不能只靠自己告警

本系统的工作就是接收告警、开事件、调查。如果把"本系统出问题"的告警也发给它自己，会有三个问题：

1. 系统挂了、卡住了或者额度用完了，正好没法处理；
2. 能处理时，它会花模型额度调查它自己；
3. 调查过程中的报错可能又产生新告警，形成循环。

所以这些规则发给人的渠道，系统自己只负责评估和展示。实验环境里还多了一层保护：本系统的 span 不经过 Demo 的 collector，这样它们不会变成 span 指标，也就不会进入实验环境的 SLO 告警（见 3.6）。

## 3. 踩过的坑

### 3.1 FastAPI 已经自带 trace

我自己在中间件里给每个请求开了一个 server span。测试里一个请求出来两层 `POST /api/v1/alerts`，外加 `fastapi.dependencies`、`fastapi.endpoint`、`fastapi.serialization`。

原因是 FastAPI 0.143 内置了 OpenTelemetry：只要全局有 tracer provider，就会按 HTTP 语义约定打 span。

改法：

- 去掉自己的 span，用 `FastAPI(telemetry=...)` 关掉 operation span 和 OTel 指标；
- 用 `exclude` 排除长连接、探针和 `/metrics`；
- 中间件只负责 Prometheus 计数。

事件根 span 的 link 因此指向 FastAPI 的 span，测试改成比较 trace id。

### 3.2 回滚的状态迁移被计数了

第一版在 `incidents.transition` 里直接计数。版本冲突（`StaleVersion`）或租约丢失时，整个事务会回滚，但计数已经加上了。

改法：观测先挂在连接上，引擎的 `commit` 事件触发时执行，`rollback` 和连接归还时丢弃。

改完又发现两个问题：

- **测试里的引擎没挂钩子**，观测永远不会执行。第一次跑，三个指标测试都是 0。
- **观测会在连接上越积越多**：`Connection.info` 跟着池里的物理连接走，没有钩子就没人清理。

所以 `after_commit` 先检查引擎是否装了钩子，没装就立即执行；测试的引擎也装上钩子。

### 3.3 Jaeger 2.21 去掉了旧的查询接口

本地验收时，Grafana 的 Jaeger 数据源健康检查返回 404。

逐个版本试下来：

- 2.20 同时提供 `/api/services` 和 `/api/v3/services`；
- 2.21 只剩 `/api/v3`。

Grafana 13.2 的 Jaeger 插件还在调用旧接口，所以 Jaeger 固定在 2.20。等 Grafana 换用新接口后再升级。

### 3.4 指标端口冲突

worker 启动时监听 9464。测试会在一台机器上起多个 worker 子进程，后起的端口冲突，进程直接退出。

改法：

- 端口被占用时只记一条警告，worker 照常工作。缺指标总比缺 worker 好。
- 测试子进程设置 `SRE_METRICS_PORT=0`。

### 3.5 本地的 `.env` 污染了测试

本地验收时我建了一个 `.env`，里面有 `SRE_AUTO_INVESTIGATE=false`。之后跑全量测试，一个测试超时：测试会起子进程 worker，`Settings` 读到了这个 `.env`，分诊后不再开始调查。

删掉 `.env` 以后测试通过。教训：本地验收用的配置，不要放在测试也会读的位置。

### 3.6 不能发给 Demo 的 collector

最省事的做法是把 trace 发给 Demo 已有的 OpenTelemetry collector，由它转给 Jaeger。但 collector 的 `span_metrics` connector 会把每个 span 变成 `traces_span_metrics_calls_total{service_name=...}`。实验环境的 SLO 规则正是 `sum by (service_name)` 这个指标，不区分服务。

后果是一条循环：

1. 本系统某个 job span 报错；
2. 报错算进 `service_name="sre-blackboard-worker"` 的错误率；
3. 触发告警，Alertmanager 发给本系统；
4. 本系统开一个事件，用模型去调查它自己。

所以给 Demo 的 Jaeger 单开一个 OTLP 的 NodePort，直接发过去。

### 3.7 沙箱里 docker build 拿不到依赖

开发容器里，`docker build` 的 `uv sync` 不信任代理的 CA，worker 镜像还要从 HuggingFace 下载模型。

本地验收改成这样：

- API（两个进程，开 multiprocess 模式）和两个 worker 直接在宿主机上跑；
- Prometheus、Grafana、Jaeger 用 host 网络；
- Prometheus 用静态目标。

CI 里用真正的镜像和 DNS 服务发现再验一遍。

## 4. 结果

### 4.1 本地验收（和 CI 同一个脚本）

压测发出 2000 条告警，分布在 200 个告警身份上，然后运行 `scripts/obs_check.py`：

```
ok    targets up: sre-api, 2 sre-worker
ok    sum(sre_alerts_received_total) = 2000
ok    sum(sre_alerts_received_total{result="opened"}) = 200
ok    sum(sre_jobs_total{kind="triage", outcome="done"}) = 200
ok    sum(sre_queue_wait_seconds_count{kind="triage"}) = 200
ok    max(sre_queue_ready_oldest_seconds) = 0
ok    11 alert and recording rules loaded, all healthy
ok    every dashboard query evaluates (39)
ok    data source sre-jaeger answers
ok    trace c27dc223…: ['sre-blackboard-api', 'sre-blackboard-worker'], incident root -> job triage
ok    replayed trace 0ff92a1e…: 71 spans {'incident': 1, 'job': 3, 'chat': 21, 'invoke_agent': 6, 'execute_tool': 39, 'policy.evaluate': 1}
```

回放的 bad-release 调查在 Jaeger 里的样子：

![回放的 bad-release 调查](../assets/m8-trace.png)

图里的时长是回放的时长（模型和工具都从录音里取），不是真实耗时。

### 4.2 开销

用 SDK 和批量导出器测了单次操作的耗时：

| 操作 | 耗时 |
|---|---|
| 一个 span（含 4 个属性） | 约 37 µs |
| 一次计数器加一次直方图观测 | 约 4 µs |

一个事件约 71 个 span，合计不到 3 ms。相比之下，诊断 p50 是 166 s。

### 4.3 还没有的数字

- **实验环境里真实模型的 trace 还没跑过**。实验环境会调用模型，消耗订阅额度，需要单独确认后再跑。到时可以把 M7 的 166 s 拆开看：多少时间花在 `chat` 上，多少花在 `execute_tool` 上，多少是等并发槽位。
- **SLO 达标率**：按 M7 的分布，默认变体大部分 P0 会超过 2 分钟，单 Agent 变体接近达标。目标本身是对的，它暴露的是差距。

## 5. 面试问答

**Q1：一次事件横跨 API、worker、执行器，进程之间只有一个 Postgres 队列，怎么做成一条 trace？**

把 W3C traceparent 当数据存起来：

1. 开事件时创建根 span，把它的 traceparent 写进事件行；
2. 入队时，任务行复制事件的 traceparent；
3. 认领任务的进程用它作父上下文，开 `job <kind>` span。

这和消息队列在消息头里带 trace 上下文是同一个思路。CI 里有一项检查：Jaeger 里的同一条 trace，根 span 来自 API，`job triage` 来自 worker。

**Q2：为什么每个事件开一条新的 trace，而不是挂在 webhook 请求下面？**

一个 webhook 请求可能带几十条告警、开出好几个事件；事件的生命周期是几分钟到几小时，请求只有几毫秒。挂在请求下面，一批事件会挤在同一条 trace 里，还会和合并进已有事件的重复告警混在一起。

所以每个事件一条新的 trace，用 link 指回请求：从请求能找到它开出的事件，从事件也能回到请求。

**Q3：审批请求入队了执行任务，这个任务挂在哪？**

挂在事件的根 span 下，不挂在审批请求下。入队时用 `COALESCE(事件的上下文, 当前 span)`，任何路径入队的任务都在事件的树上。

否则，同一个事件的执行和核验会出现在审批请求的 trace 里，"一个事件一棵树"就不成立了。测试专门覆盖了这一点。

**Q4：Agent 的 trace 里记了什么，没记什么？**

记了：

- 每个 Agent 一个 `invoke_agent` span：模型、提示词版本、步数、工具调用次数、改写次数、丢弃的发现、token；
- 每次模型调用一个 `chat` span：GenAI 约定的 token 属性、缓存读写、时延、重试次数、请求哈希；
- 每次工具调用一个 `execute_tool` span：参数、证据 id、错误码。

没记：提示词和回答。原因有两个：

- 量大，一步就是几万 token 的上下文；
- 可能包含日志里的敏感数据。

它们已经在 `llm_calls` 表里，用请求哈希关联。工具参数（PromQL、LogQL）记了：排查时最先要看的就是 Agent 问了什么，而且参数里没有密钥。

**Q5：队列深度为什么由 API 从数据库现查，而不是由 worker 上报？**

真相在数据库里。worker 上报有两个问题：

- 多个 worker 报的是同一个数；
- worker 全挂的时候，这些数字也跟着消失。

"没有 worker、队列在涨"恰恰是最需要告警的时刻。由 API 现查，只要 API 和数据库在，这些数字就在。

每个优先级都输出（没有就是 0），告警规则看到的是 0，不会因为序列不存在而静默。

**Q6：如果事务回滚了，指标怎么办？**

状态迁移和事务里的其他写入要么一起提交，要么一起回滚。指标如果在迁移函数里立即加上，回滚后就多算了一次。

本系统把观测挂在连接上：

- 引擎的 commit 事件触发时执行；
- rollback 或连接归还时丢弃。

有一个实现细节：`Connection.info` 跟着池里的物理连接走，所以必须确认引擎装了钩子，否则观测会在连接上越积越多。

**Q7：指标的标签怎么选？**

只用取值有限的集合：任务种类、状态、优先级、Agent 角色、工具名、模型、错误码。

- 租户、事件 id、服务名不作标签。它们会让序列数随数据增长，而且按单个事件看问题本来就该用 trace。
- 模型输出的工具名不能直接当标签，否则模型编一个名字就多一条序列，统一记成 `unknown`。

**Q8："P0 首次诊断不超过 2 分钟"的告警怎么设计？**

分成两条：

- **page**：此刻有 P0 超过 2 分钟还没诊断。数据来自数据库的 gauge，针对单个事件，人手接管还来得及。
- **ticket**：最近一小时 2 分钟内诊断的比例低于 90%。用直方图 `le="120"` 的桶除以总数算，升级算作未达标。

为了精确计算，120 必须是一个分桶边界。

M7 的实测数据说明默认变体达不到这个目标（p50 166 s）。目标不该迁就现状：它说明要么换单 Agent 变体，要么缩短调查。

**Q9：系统自己的告警发给谁？**

发给人，不发给系统自己：

- 系统挂了，就处理不了关于自己的告警；
- 系统没挂，就会花模型额度调查自己；
- 调查过程中的报错还可能触发新的告警，形成循环。

实验环境里还要防一个间接的循环：Demo 的 collector 会把 span 变成 span 指标，实验环境的 SLO 告警按服务名统计。所以本系统的 trace 直接发给 Jaeger，不经过 collector。

**Q10：怎么在 CI 里验证可观测性，又不调用模型？**

两步：

1. **复用 M1 的压测**：2000 条告警、200 个事件，跑完以后在真实的 Prometheus 里对账：告警数、事件数、分诊任务数、队列清空。然后：
   - 每条告警规则和面板查询都在 Prometheus 上求值一遍，PromQL 写错会在这里失败，不会等到值班时才发现；
   - 检查 Grafana 加载了仪表盘，两个数据源都正常；
   - 检查 Jaeger 里有跨 API 和 worker 的 trace。
2. **回放 M7 的一段录音**：worker 的处理函数、Agent 循环、策略都是真的在跑，只有模型和工具从录音里回答。span 导出到同一个 Jaeger，检查树的形状和 GenAI 属性。

**Q11：可观测性本身的开销有多大？**

- 一个 span 约 37 µs，导出是批量、后台线程完成的；
- 一次计数器加直方图约 4 µs。

一个事件约 71 个 span，合计不到 3 ms；相比之下，诊断要两三分钟。真正要管的不是 CPU，而是：

- **序列基数**：标签有界；
- **trace 存储量**：事件量大了以后做尾部采样，保留慢的和失败的。

**Q12：怎么用 trace 调试一次"诊断错了"的调查？**

1. 在 Jaeger 里按 `sre.incident.id` 找到这条 trace；
2. 看 `invoke_agent synthesizer` 之前各个调查员的 `execute_tool` 参数：有没有查对服务、对时间窗；
3. 看哪个工具返回了错误码，有没有因为 `too_large` 被迫缩小查询；
4. 看哪一步 `chat` 的 `sre.llm.final` 为真，以及被丢弃了几条发现；
5. 需要看模型原话时，用 `sre.llm.request_hash` 去 `llm_calls` 表里取回答。
