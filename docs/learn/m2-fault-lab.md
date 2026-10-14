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

告警响在哪个服务，不等于问题出在哪个服务。例如给 product-catalog 注入网络延迟：product-catalog 自己的 SERVER span 并不变慢（延迟加在它的出口网络上），变慢的是调用它的 frontend。告警会响在 frontend（Event Host），根因却是 product-catalog（Problem Host）。

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

## 4. 验收结果

（待 GitHub Actions 运行完成后填写。）

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
