# ADR-0004：故障实验环境用 kind + OpenTelemetry Demo，在 GitHub Actions 上运行

- 状态：已接受
- 日期：2026-10-14
- 相关里程碑：M2（以及依赖它的 M3、M4、M5、M7）

## 背景

诊断能力要在"真的坏掉"的系统上验证，不能只靠手写的假数据。约束有三条：

- 本地机器只有 16 GB 内存，而且平时被其他项目占着，没法长期开一个集群；
- 云端开发容器跑不了 Kubernetes（kind 和 k3s 都会在设置 `oom_score_adj` 时失败）；
- 单人维护，环境必须能一条命令从零建起来，不能依赖手工步骤。

## 决定

**集群和被测系统**

1. **单节点 kind 集群**，版本钉死在 `deploy/kind/versions.env`。节点镜像带 digest。
2. **OpenTelemetry Demo**，Helm chart 版本钉死为 0.42.4（应用版本 3.1.0），并做了裁剪：
   - 关掉 Kafka 的两个消费者（accounting 是 .NET、fraud-detection 是 JVM）、Demo 自带的三个 LLM 组件（agent、chatbot、mcp）、telemetry-docs 和 Grafana；
   - Kafka 本身保留，因为 checkout 下单时要往里写；
   - load-generator 关掉浏览器流量，Chromium 太占内存，HTTP 用户已经能覆盖所有服务。
3. **日志用 Loki 代替 OpenSearch**：单二进制部署，数据放在 emptyDir 里。OpenSearch 的内存上限是 1.1 GB，Loki 是 0.4 GB。collector 通过 OTLP 写入，`service.name` 会变成 Loki 的 `service_name` 标签。
4. **Prometheus 和 Alertmanager 直接用 Demo chart 自带的**：打开 alertmanager 子 chart，告警规则作为普通规则文件挂进去。计划初稿写的是 `PrometheusRule`，那需要 Prometheus Operator，也就是再装一整套 kube-prometheus-stack，这里没有必要。规则文件本身是标准格式，以后迁移到 Operator 也很简单。

**告警**

5. **SLO 告警规则基于 span 指标**（collector 的 `span_metrics` connector），只统计 SERVER span，也就是"调用方实际感受到的"。
   - 可用性：多窗口燃烧率。窗口按 Google SRE workbook 的方案缩短 12 倍：快速燃烧看 5m 和 1m，慢速燃烧看 30m 和 5m，这样故障几分钟内就能告警；
   - 延迟：p95 超过 1 秒；
   - 快、慢两档用同一个告警名、靠 `burn` 标签区分。因为本服务按"告警名 + 服务 + 环境"算指纹，两档会合并进同一个事件。如果分开命名，快速燃烧恢复后慢速燃烧还会再响几分钟，凭空多出一个事件。
   - 规则用 `promtool test rules` 做单元测试，在 CI 里运行。
6. **Alertmanager 把告警推给跑在集群外的本服务**：
   - 集群里建一个没有 selector 的 Service `sre-blackboard`，配一个 EndpointSlice，指向宿主机。宿主机地址在 Docker Desktop 上取 `host.docker.internal`，在 Linux 上取 kind 网络的网关；
   - 鉴权用 `up.sh` 现签的 ingest 角色 token，租户为 `lab`，放在 Secret 里挂给 Alertmanager。

**故障注入**

7. 三种方式，都在 `scripts/lab/faults.py` 里登记：
   - **feature flag**：通过 flagd-ui 的 HTTP API 修改，经由 frontend-proxy 的 `/feature` 路径访问。flag 文件放在 flagd 和 flagd-ui 共享的 emptyDir 里，改 ConfigMap 不会生效；直接写文件会和 flagd-ui 内存中的状态冲突；
   - **Chaos Mesh**：网络延迟、Pod 失效、CPU 压力，删掉对象就恢复；
   - **坏版本发布**：patch checkout 的环境变量，让它连一个不存在的端口，同时写入 `change-cause`，在 `rollout history` 里留下记录。恢复用 `kubectl rollout undo`，这正是 M5 执行器将要提议的动作。

**运行位置**

8. **在 GitHub Actions 的托管运行机上运行**（工作流 `lab.yml`）。公开仓库的运行机是 4 核、16 GB，和本地预算一致。
   - 每次运行都从零建集群，跑完验收就销毁，所以每次运行同时也验证了 `make env-up` 能从零跑通；
   - 云端开发容器读不到 Actions 的日志和产物，它们在 Azure Blob 上，被代理拦截。所以关键结果以 annotation 的形式发布，用 REST API 就能读到。GitHub 每个步骤只保留 10 条 notice，所以分三个步骤发布：结果、集群状态、日志。

## 实测（2026-10-14）

- 3 个代表性故障（支付失败、网络延迟、坏版本发布）全部通过：首个告警在 179–203 秒到达本服务，恢复后 306–330 秒告警解除。解除慢是 5 分钟窗口决定的，详见 [M2 学习笔记](../learn/m2-fault-lab.md#4-验收结果)。
- 内存：kind 节点 4.3 GiB（工作集），本服务 0.3 GiB，运行机共用 4.9 GiB / 15.6 GiB。16 GB 的本地机器放得下。
- 一次完整运行约 40 分钟，其中验收 29 分钟。

## 考虑过的方案

| 方案 | 优点 | 缺点 | 结论 |
|---|---|---|---|
| 只在本地跑 kind | 交互调试最快 | 本地机器长期被占用，项目会卡在这里 | 不作为主路径；`make env-up` 照样支持本地 |
| Demo 的 docker compose 版本 | 最轻 | 没有 Deployment，也就没有 rollout 历史，回滚闭环（M5）没法演示 | 不采用 |
| kube-prometheus-stack | `PrometheusRule`、ServiceMonitor 齐全 | 多一个 Operator 和一大批 CRD，内存约多 0.5 GB；Demo 已经自带 Prometheus | 不采用 |
| 自托管 runner 或云上的虚拟机 | 可以长期开着，方便交互调试 | 要花钱、要运维，还有安全面（公开仓库配自托管 runner 有风险） | 不采用 |
| GitHub Actions 托管运行机 | 免费、可复现、每次都是干净环境 | 不能交互调试，一次迭代 15–25 分钟；单个任务最长 6 小时 | **采用** |

## 后果

- 正面：
  - 整个环境是代码。一个提交就能证明"从零建环境、注入、告警、恢复"这条链路是通的；
  - 告警规则、Alertmanager 配置、Chaos Mesh 清单都能在没有集群的情况下静态验证：promtool、amtool、kubeconform 加上从 CRD 生成的 schema。大部分错误在推送前就能发现。
- 代价：
  - 调试慢。失败时只能靠 annotation 和产物里的诊断信息定位，所以诊断信息要一次收集全；
  - 运行机是共享硬件，时延类的数字（告警延迟、p95）会有波动，不适合当精确的基准；
  - 如果 GitHub 调整免费运行机的规格，资源预算需要重新评估。
- 什么情况下应该重新考虑：
  - 需要长时间连续运行，比如 M7 的大规模评测超过 6 小时；
  - 需要人工在环境里交互排查。这时候在本地起 `make env-up`，配置完全相同。
