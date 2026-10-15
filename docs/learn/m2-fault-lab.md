# M2 学习笔记：故障实验环境、SLO 告警与故障注入

> 对应里程碑：M2 · 相关决策：[ADR-0004](../adr/0004-fault-lab-on-kind-in-ci.md)
> 这一阶段造了一个"能真的坏掉"的系统：一个电商微服务应用跑在 Kubernetes 里，按 SLO 告警，告警推给本服务。之后 Agent 的每一次诊断、每一个评测分数，都建立在这个环境之上。

## 1. 这一阶段做了什么

| 类别 | 内容 |
|---|---|
| 集群 | kind 单节点；版本全部钉死（`deploy/kind/versions.env`）；`make env-up` 幂等，重复执行只应用变更 |
| 被测系统 | OpenTelemetry Demo 3.1.0（chart 0.42.4），裁剪掉约 2 GB 用不上的组件 |
| 可观测性 | 指标：Prometheus（OTLP 写入）；链路：Jaeger；日志：Loki（替换 OpenSearch） |
| 告警 | 基于 span 指标的 SLO 规则：多窗口燃烧率 + p95 延迟；promtool 单元测试；Alertmanager 带 token 推送到本服务 |
| 故障注入 | 15 种故障：11 个 feature flag、3 种 Chaos Mesh、1 个坏版本发布；`make fault` / `make recover` |
| 验收 | `scripts/lab/accept.py`：注入 → 5 分钟内收到告警 → 恢复 → 告警解除；在 GitHub Actions 上从零跑 |

## 2. 背景知识

### 2.1 SLI、SLO 与错误预算

- **SLI**（指标）：一个比例，例如"成功的请求数 / 总请求数"。
- **SLO**（目标）：SLI 要达到的水平，例如 30 天内 99%。
- **错误预算**：1 − SLO，即允许失败的比例，这里是 1%。预算是一种"可以花的东西"：发布、实验都会花掉一些。花得太快才需要叫人。

本项目的 SLI 来自 span 指标：collector 的 `span_metrics` connector 把每个 span 计数，按服务、span 名、span 类型、状态码分组，生成 `traces_span_metrics_calls_total` 和耗时直方图。只统计 **SERVER span**，因为它代表"调用方实际感受到的结果"；CLIENT span 是调用别人，算在被调用方头上会重复。

### 2.2 燃烧率与多窗口告警

**燃烧率** = 当前错误比例 / 错误预算比例。燃烧率为 1，表示刚好在 30 天用完预算；为 14.4，表示约 2 天就会用完。

只看一个窗口会有两个问题：

- 窗口长（1 小时）：故障开始后要很久才触发；故障结束后也要很久才恢复。
- 窗口短（5 分钟）：一点抖动就会误报。

**多窗口**的做法是长短两个窗口同时超阈值才告警：长窗口保证"确实烧了不少预算"，短窗口保证"现在还在烧"。故障一结束，短窗口立刻降下来，告警很快解除。

| 档位 | SRE workbook | 本项目（缩短 12 倍） | 严重度 |
|---|---|---|---|
| 快速燃烧 | 14.4 倍，1h 和 5m | 14.4 倍，5m 和 1m | critical（P1） |
| 慢速燃烧 | 6 倍，6h 和 30m | 6 倍，30m 和 5m | warning（P2） |

窗口缩短是为了让实验在几分钟内出结果，代价是对短时抖动更敏感。另外所有告警都有流量门槛：5 分钟内少于 10 个请求就不告警，避免"3 个请求错 1 个"就把人叫起来。

### 2.3 告警名和事件指纹

本服务按"告警名 + 服务 + 环境"计算事件指纹（M1）。这个看似细节的选择影响了规则怎么写：

- 如果快、慢两档用两个告警名，一次大故障会产生两个事件。而且快速燃烧解除后，慢速燃烧还要再响几分钟，恢复期间又冒出一个新事件。
- 所以两档用同一个名字 `ErrorBudgetBurn`，用 `burn` 标签区分，进同一个事件。Alertmanager 另有一条抑制规则：快速燃烧在响时，压住同服务的慢速燃烧通知。

这一点是在写 promtool 测试时发现的：测试显示大故障时两档都会触发，恢复后慢速燃烧还在。规则的单元测试能抓住这种设计问题，而不只是语法错误。

### 2.4 Problem Host 与 Event Host

告警响在哪个服务，不等于问题出在哪个服务。例如给 product-catalog 注入网络延迟，实测告警响在 5 个服务上：product-catalog 自己，以及直接或间接调用它的 recommendation、checkout、frontend、frontend-proxy（Event Host），根因只有 product-catalog（Problem Host）。反过来，给 checkout 发一个坏版本，告警只响在 checkout 上，调用它的 frontend 并没有响，因为下单只占 frontend 流量的约 3%，到不了燃烧阈值。

故障目录给每个故障同时记录了 `root_cause`（Problem Host）和 `symptoms`（预期告警的服务）。这正是原型的 B10（把告警主机当成根因主机）要修的问题，也是 M4 诊断要解决的核心难点。

### 2.5 三种故障注入方式

| 方式 | 例子 | 原理 | 恢复 |
|---|---|---|---|
| feature flag | `paymentFailure` | 应用代码里预埋的故障分支，flagd 下发开关 | 开关设回 `off` |
| Chaos Mesh | 网络延迟、Pod 失效、CPU 压力 | 在节点上用 tc、cgroup 等手段改变容器的运行条件 | 删除 Chaos 对象 |
| 坏版本发布 | checkout 配错 payment 端口 | 一次真正的 Deployment rollout，留下 rollout 历史和 `change-cause` | `kubectl rollout undo` |

三种方式覆盖了不同类型的根因：代码缺陷、基础设施问题、变更引起的故障。其中"变更引起"在真实事故里占比最高，也是"回滚"这个动作能解决的那一类。

### 2.6 集群里的服务怎么访问集群外的进程

本服务跑在集群外（docker compose），Alertmanager 在集群里，要推送告警给它：

- 建一个**没有 selector 的 Service**，Kubernetes 就不会自动维护它的后端；
- 再手动建一个 **EndpointSlice**，把后端写成宿主机的 IP；
- 宿主机 IP：Linux 上是 kind 所在 Docker 网络的网关；Docker Desktop 上用 `host.docker.internal` 解析。

这样 Alertmanager 的配置里只写 `http://sre-blackboard:8000`，不关心宿主机在哪。以后本服务部署进集群，只需要把 Service 改成有 selector 的普通 Service，Alertmanager 配置不用动。

## 3. 踩过的坑，以及设计时预先规避的坑

| 坑 | 现象或风险 | 处理 |
|---|---|---|
| Demo 版本比印象中新得多 | 3.x 新增了 LLM 组件，product-catalog 改读 Postgres，flag 改用 flagd-ui 管理 | 先读 chart 源码再写配置，不凭记忆（预先规避） |
| flag 文件不在 ConfigMap 里生效 | flagd 从 emptyDir 读文件，初始化容器从 ConfigMap 复制一次 | 改用 flagd-ui 的 `/api/write`；直接写文件还会和 flagd-ui 内存状态冲突（预先规避） |
| 快慢燃烧两个告警名 | 一次故障两个事件；恢复期间多出一个 | 同名 + `burn` 标签 + 抑制规则；promtool 测试发现 |
| span 指标默认 60 秒刷新一次 | 1 分钟窗口里只有 1 个点，`rate` 算不出来 | `metrics_flush_interval: 15s`（预先规避） |
| Prometheus 没有配置重载 sidecar | 改了规则 ConfigMap，Prometheus 不会重新加载 | 规则内容的哈希写进 Pod 注解，规则变了 Pod 才滚动（预先规避） |
| `rollout undo` 不还原 Deployment 自己的注解 | 恢复后 `change-cause` 还是坏版本的说明，"是否已注入"判断出错 | 恢复后把注解改成"rollback of: …" |
| 验收脚本漏掉恢复期间的 resolved 通知 | 恢复时间点在 `recover()` 之后才取，期间到达的通知被漏掉 | 在恢复前取时间；判断每个指纹"最后一条"通知是否为 resolved，同时处理告警抖动 |
| 云端读不到 Actions 日志 | 日志和产物在 Azure Blob 上，被代理拦截 | 关键结果写成 annotation，REST API 可读 |
| `ubuntu-latest` 即将切到新系统 | lab 环境可能无意中改变 | 固定 `ubuntu-24.04` |
| 第一次运行在建环境时退出 | `getent` 查不到 `host.docker.internal` 时返回非零，在 `pipefail` + `errexit` 下整个脚本退出 | 这一行加 `\|\| true`，失败时退回到网关地址 |
| annotation 只出来 10 条 | GitHub 每个步骤最多保留 10 条 notice，多出的直接丢弃，不报错；日志全丢了 | 结果、集群状态、日志分三个步骤发布 |
| cgroup 内存显示 10 GB | `memory.current` 包含页缓存，看起来快用满了 | 以 `docker stats`（工作集）为准，另记录每个容器的工作集 |
| 预期症状写错了 | 凭直觉写的"网络延迟只影响上游"，实测 product-catalog 自己也告警 | 读源码确认原因，按实测更新故障目录 |
| 第 4 次运行"全绿"，其实是错的 | Demo 默认 5 个虚拟用户，checkout、payment 每分钟只有约 3 个请求。1 分钟窗口的错误比例忽高忽低，流量门槛也时过时不过，支付失败的告警在故障期间消失了一次，恢复后又响了，被记到了下一个故障（网络延迟）头上 | 虚拟用户加到 20；验收收紧（见下一行） |
| 验收脚本太宽松 | 只看"某一刻没有告警"就算安静；不管 pending；不要求告警响在预期的服务上；不检查抖动 | "安静"要求连续 60 秒没有 firing 也没有 pending；告警抖动（故障期间解除、解除后又响）或没有响在预期服务上，都判失败 |
| 第 5、6 次运行通过了，但基线就在告警 | 故障注入前 frontend 的错误就有 7%–13%，慢速燃烧在响；验收只等"安静下来"再开始，没把它当问题 | 验收先空跑 10 分钟，有任何告警直接失败；根因见第 4 节 |
| 启动噪声 | Demo 没有 readiness probe，load-generator 在后端开始监听前就发请求，ECONNREFUSED 后 gRPC 客户端进入重连退避，错误持续几分钟，30 分钟窗口又留住了它 | load-generator 加 init 容器，等所有后端能连上再开始 |
| 日志里看不到错误详情 | Loki 返回的日志行只有消息本身，异常信息在 structured metadata 里 | 查询时加 `categorize-labels` 请求头；同时从 Jaeger 拉错误链路 |
| 快慢燃烧共用事件指纹 | 本服务按"告警名 + 服务 + 环境"算指纹，快慢两档是同一个；恢复时"快速解除、慢速出现"会被误判成抖动 | 验收脚本按 Alertmanager 的完整标签集区分告警 |

## 4. 验收结果

2026-10-15，第 8 次运行（GitHub Actions 运行机 `ubuntu-24.04`，4 核 16 GB），从零建环境。先空跑 10 分钟确认没有任何告警，再依次运行 3 个代表性故障：

| 故障 | 根因 | 方式 | 首个告警 | 告警（服务） | 恢复后告警解除 |
|---|---|---|---|---|---|
| payment-failure | payment | flag，50% 支付失败 | 221 秒 | ErrorBudgetBurn（payment、checkout） | 290 秒 |
| network-delay | product-catalog | NetworkChaos，出口加 1 秒延迟 | 156 秒 | LatencyP95High（recommendation、product-catalog、frontend、frontend-proxy、checkout） | 331 秒 |
| bad-release | checkout | rollout，payment 地址改成错误端口 | 130 秒 | ErrorBudgetBurn（checkout） | 308 秒 |

三个都满足：5 分钟内告警、至少一个告警响在预期的服务上、没有抖动、恢复后全部解除且实验环境重新安静 60 秒。整个任务 54 分钟：清理磁盘 3 分钟，建环境 2.5 分钟，等 SLI 窗口填满 4.5 分钟，验收 43 分钟（其中基线 10 分钟）。

第 5–8 次运行在同样的收紧后的验收下都通过了，首个告警的范围：支付失败 128–221 秒，网络延迟 156–188 秒，坏版本发布 130–149 秒。支付失败波动最大，因为 payment 每秒只有约 0.2 个请求，1 分钟窗口里只有十几个样本。221 秒离 5 分钟的上限还有 79 秒余量，但如果以后它开始超时，首先要看的就是流量。

**时间都花在哪里**（以支付失败为例，错误比例 50%，快速燃烧阈值 14.4%）：

| 环节 | 大约耗时 |
|---|---|
| span 指标刷新（15 秒）+ Prometheus 抓取和规则评估（15 秒） | 0–30 秒 |
| 5 分钟窗口的错误比例爬过 14.4% | 约 90 秒 |
| `for: 1m` | 60 秒 |
| Alertmanager `group_wait` | 10 秒 |
| 合计 | 约 190 秒；流量低时 1 分钟窗口会短暂跌回阈值以下，`for` 重新计时，所以实测 128–221 秒 |

**恢复为什么要 5 分钟左右**：快速燃烧的 1 分钟窗口很快降下来，但快速燃烧一解除，被抑制的慢速燃烧就露出来了。它看的 5 分钟窗口要等故障期间的错误被"挤出"窗口，比例从 50% 降到 6% 以下，大约 4.4 分钟，再加上刷新、评估和 `group_interval`。延迟告警同理：5 分钟内慢请求的占比要降到 5% 以下，p95 才回到阈值下面。这是窗口长度决定的，不是故障没恢复。如果想更快解除，可以缩短慢速燃烧的短窗口，代价是更容易抖动。

**观察到的现象**

- **告警响在哪里**：网络延迟加在 product-catalog 的出口上。我原以为它自己的 SERVER span 不会变慢，实测却变慢了：读源码发现它每个请求都要查 Postgres，GetProduct 还要问 flagd，这些出口请求都被加了延迟。调用它的 recommendation、frontend、frontend-proxy 也响了；流量加到 20 个用户后，checkout 也过了流量门槛（下单时要逐个查购物车里的商品），同样响了。5 个告警、5 个事件，根因只有一个，这正是 M4 要做的聚合和定位。故障目录里的预期症状按实测结果做了修正。
- **支付失败**同时在 payment 和 checkout 上告警：payment 自己返回错误，checkout 的下单请求跟着失败。坏版本发布只在 checkout 上告警，因为 payment 根本没收到请求。两者症状相似，区别在 payment 有没有出错：这是诊断时区分"下游坏了"和"自己配错了"的关键证据。
- **基线**（无故障）：所有服务的错误比例都是 0，p95 最高的是 product-catalog 17 ms，离 1 秒的阈值很远。之前几次运行里 frontend 有 0.7%–13% 的错误，查下来是启动噪声（见下）；recommendation 的 p95 也到过 580 ms，修复启动噪声后没再出现，没有单独确认原因。
- **流量**：20 个用户时，流量最小的 checkout、payment、email、quote 每秒约 0.19 次，5 分钟约 56 次，是 10 次门槛的 5 倍多。

**启动噪声是怎么查出来的**

第 5、6 次运行三个故障都通过了，但 `status` 显示：故障还没注入，frontend、frontend-proxy、checkout 的慢速燃烧告警已经在响，frontend 的错误比例在 7%–13%，每次都不一样。排查过程：

1. 按 span 名拆错误：集中在 frontend 的 `GET /api/products/[productId]`，frontend 这一侧 GetProduct 的 CLIENT span 报错，但 product-catalog 的 SERVER span 和它查数据库的 span 都没有错误。所以请求根本没到 product-catalog。
2. 日志行本身只有 "API request failed"，错误详情在 Loki 的 structured metadata 里，要加 `X-Loki-Response-Encoding-Flags: categorize-labels` 请求头才返回。拿到之后是 `14 UNAVAILABLE: No connection established. Last error: connect ECONNREFUSED`，目标是 product-catalog、cart、checkout、shipping 的 ClusterIP。Jaeger 里的错误链路给出了同样的结论。
3. 没有 Pod 重启过，所以是启动阶段的问题：Demo 的服务都没有 readiness probe，容器一启动 Service 就把流量转过来，而 Java、.NET 服务和要等 Kafka、Postgres 的服务要几十秒才开始监听。load-generator 一上来就发请求，被拒绝；frontend 的 gRPC 客户端连接失败后进入重连退避（最长 2 分钟），退避期间每个调用都立刻失败，所以错误能持续好几分钟；慢速燃烧看 30 分钟窗口，把这些错误又留了很久。
4. 修复：给 load-generator 加一个 init 容器，等 frontend 和它背后的所有服务都能建立 TCP 连接后再开始发流量。告警规则和阈值都没有动。第 8 次运行的基线所有服务错误为 0。

同时验收加了一道门：注入任何故障前，先空跑 10 分钟，期间出现任何告警（包括 pending）就直接失败。第 5、6 次运行能"通过"，就是因为验收只等实验环境安静下来再开始，不把基线的告警当问题。

**资源占用**（基线，故障注入前）

| 项 | 实测 |
|---|---|
| kind 节点容器（集群里全部组件，`docker stats` 口径，不含可回收的页缓存） | 4.4 GiB |
| 本服务（Postgres、API、2 个 worker） | 0.3 GiB |
| 运行机已用内存（`free`） | 5.0 GiB / 15.6 GiB |

占用最多的容器（工作集）：kube-apiserver 586 MiB、Kafka 494 MiB、ad（Java）273 MiB、Prometheus 246 MiB、flagd-ui 145 MiB、collector 113 MiB、Loki 106 MiB。用户数从 5 加到 20，kind 节点只从 4.2 GiB 涨到 4.4 GiB；CPU 在 4 核里用了约 0.75 核。

kind 节点 cgroup 的 `memory.current` 是 10.2 GiB，但这个数字包括页缓存（镜像层、日志文件），内存紧张时内核会回收，不能当作占用。`docker stats` 用的是"使用量减去非活跃文件缓存"，和 Kubernetes 判断 OOM 用的工作集口径一致。

结论：整个实验环境加本服务约 5 GB，16 GB 的本地机器还能留出约 10 GB 给系统和其他程序。计划里集群部分的估算是 4.6–5.6 GB（Demo 3–4 GB，加 Loki、Prometheus、Jaeger、Alertmanager、Chaos Mesh），实测 4.4 GiB（4.7 GB），落在估算的下限。省下来的主要是 OpenSearch、两个 Kafka 消费者（JVM 和 .NET）和 load-generator 的浏览器流量。

## 5. 面试问答

**Q1：为什么用多窗口燃烧率告警，而不是"错误率超过 5% 就告警"？**
答：固定阈值和预算无关：5% 对 99.9% 的服务来说是灾难，对 95% 的服务来说是常态。燃烧率把告警和"预算还能撑多久"挂钩，严重度有统一含义。多窗口是为了同时满足两点：长窗口保证不是抖动，短窗口保证故障结束后告警能很快解除。

**Q2：你的告警窗口为什么和 SRE workbook 不一样？**
答：缩短了 12 倍，因为实验环境要在几分钟内出结果，而 workbook 的 1 小时窗口要几十分钟才触发。比例关系保持不变，代价是对短时抖动更敏感，所以我加了流量门槛。生产环境会用原始窗口。

**Q3：为什么用 span 指标做 SLI，而不是各服务自己暴露的指标？**
答：Demo 的服务用了七八种语言，各自的指标命名不一致；span 指标由 collector 统一生成，所有服务口径一致。缺点是依赖链路采样和 collector 的健康：collector 挂了，SLI 也就没了。生产中还会用负载均衡器或网关的指标做一层兜底。

**Q4：告警响在 frontend，你怎么知道根因不在 frontend？**
答：这正是诊断要回答的问题。告警只说明"哪里感受到了问题"。要沿着调用链往下找：frontend 的 trace 里哪个下游 span 变慢或出错，那个下游自己的 SERVER span 是否也异常；还要看最近的变更、Pod 事件。我的故障目录给每个故障同时标注了根因服务和预期的告警服务，评测时就能检查 Agent 有没有把 Event Host 当成 Problem Host。

**Q5：为什么故障注入要有三种方式？**
答：它们对应不同类型的根因。feature flag 模拟代码缺陷；Chaos Mesh 模拟基础设施问题，比如网络和资源；坏版本发布模拟变更引起的故障。只有最后一种会留下 rollout 历史，回滚才是正确动作。如果评测只有 flag 类故障，"回滚"永远不是正确答案，安全策略就测不出来。

**Q6：实验环境为什么放在 CI 里跑？**
答：我的本地机器长期被占着，而云端开发容器跑不了 Kubernetes。GitHub 给公开仓库的运行机有 16 GB，和本地预算相同。每次运行都从零建环境，所以每次运行同时证明了"环境即代码"是真的。代价是不能交互调试、一次迭代要 15–25 分钟，所以我尽量在推送前做静态验证：promtool 测规则、amtool 查 Alertmanager 配置、kubeconform 加 CRD schema 校验清单、helm template 渲染检查。

**Q7：集群里的 Alertmanager 怎么调用集群外的服务？怎么鉴权？**
答：一个没有 selector 的 Service 加手工维护的 EndpointSlice，指向宿主机。Alertmanager 用 Bearer token，token 由部署脚本现签，角色是 ingest、租户是 lab，放在 Secret 里挂给 Alertmanager，配置里用 `credentials_file` 引用，不出现在 ConfigMap 里。

**Q8：怎么验证告警规则是对的？**
答：两层。第一层是 promtool 单元测试：构造输入时间序列，断言某个时刻哪些告警在响、标签和注解是什么，包括"低流量不告警""CLIENT span 不计入""故障恢复后多久解除"。第二层是在真实环境里注入故障，看告警有没有在 5 分钟内到达本服务、恢复后有没有解除。第一层快且确定，第二层慢但真实。
