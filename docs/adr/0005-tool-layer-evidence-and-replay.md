# ADR-0005：工具层只读、结果存证据快照、按"调用 → 原始结果"录制回放

- 状态：已接受
- 日期：2026-10-15
- 相关里程碑：M3（M4 的 Agent、M7 的评测都建立在它上面）

## 背景

M4 的 Investigator 要查真实的指标、日志、链路和 Kubernetes 状态。工具层要同时满足四件事：

- **放得进 prompt**：一次链路查询的原始结果有几百 KB，一次日志查询有几百行；订阅额度有限，每一步都要重发 transcript。
- **可追溯**：结论必须能指回具体的数据。原型的工具返回伪造的 `EV-xxxxx`，引用无从核对（问题清单 1.1）。
- **可测试**：CI 和云端开发容器里没有集群；LLM 本身也不确定。没有确定性的工具层，Agent 就没法回归测试。
- **安全**：Agent 只能读，不能改；也不能读到实验环境自己的"答案"。

## 决定

**工具的形状**

1. 每个工具拆成两半：`fetch` 访问后端，返回原始 JSON；`summarize` 把原始结果压成有界的摘要。参数是 Pydantic 模型（`extra="forbid"`），同一份 schema 直接作为 Claude tool use 的 `input_schema`。
2. 9 个工具全部是 R0（只读）：`prom_query`、`prom_range`、`logs_search`、`traces_search`、`trace_get`、`k8s_events`、`rollout_history`、`deploy_diff`、`flag_state`。日志、链路的服务名用正则校验后再拼进查询，用户文本先转义，不拼接原始 LogQL。
3. 后端用 httpx 直接调 HTTP API。Kubernetes 也走 API server，用 ServiceAccount 的 bearer token，不用 kubectl 子进程，也不引入 kubernetes Python 客户端。

**调用流程**（`ToolRegistry.call`，永远返回结果，不向 Agent 抛异常）

4. 校验参数 → 取数 → 超过大小上限就拒绝（提示缩小查询）→ 生成摘要 → 用通用的 `bound()` 兜底到 6000 字符（先砍最大的列表的后半段）→ 存证据 → 记调用日志。
5. 失败统一成结构化错误：`code`（`invalid_args`、`bad_query`、`timeout`、`circuit_open` 等）、后端给的原因、是否值得重试。查询写错时，Prometheus 的报错原文会回到 Agent 手里，它可以改了再查。
6. 弹性：每次尝试有超时；只有"可能自己好"的错误（超时、连不上、429、5xx）才重试，最多 2 次，指数退避加抖动；每个后端一个熔断器，连续 5 次失败后打开 30 秒，期间立即返回 `circuit_open`。查询写错不算后端故障，不计入熔断。

**证据**

7. 每次成功调用写一行 `evidence`：原始结果（jsonb）、原始结果 canonical JSON 的 sha256、摘要、来源（live / replay）、取数时间。数据库触发器拒绝对它的 UPDATE 和 DELETE。每次调用（包括失败的）另记一行 `tool_calls`。
8. 证据 ID 由内容派生：`ev_` + sha256(范围, 工具, 参数, 结果哈希) 的前 16 位，范围默认是"租户/事件"。同一个事件里同样的数据只有一行；任务重试时复用已有的证据；回放运行时 Agent 看到的 transcript 和录制时逐字节相同，这是 M4 按 transcript 哈希回放 LLM 的前提。

**录制回放**

9. 在取数这一层录：cassette 是 JSON lines，每行是"工具 + 校验后的参数 → 原始结果（或错误）+ 取数时间"。回放时把原始结果重新交给 `summarize`，所以摘要和哈希逐字节一致；同一个调用出现多次时按录制顺序返回，用完后一直返回最后一个。
10. 录制在 GitHub Actions 的实验环境里产生：验收在每个故障进行中调用工具、检查结果、录下来，再推到 `lab-recordings/run-<id>` 分支。云端开发容器读不到 Actions 的产物，但能 `git fetch`。

**实验环境里的边界**

11. 工具以 ServiceAccount `sre-blackboard-reader` 访问 Kubernetes：只能在 Demo 的命名空间里 get/list events、pods、services、deployments、replicasets，不能读 Secret，不能写，没有集群级权限。token 由 `make lab-env` 现签，默认 12 小时过期。
12. Chaos Mesh 产生的事件在取数时就丢掉，不进证据，RBAC 也不给 Chaos Mesh 对象的读权限。它们直接写着"对 product-catalog 注入了网络延迟"，真实系统里不会有；让 Agent 看到等于把答案递给它，评测就没有意义了。坏版本发布写的 change-cause 是一条正常的发布说明，Agent 可以看到。feature flag 被打开也算一种"变更"，`flag_state` 照常提供；M7 可以做去掉它的消融实验。

## 考虑过的方案

| 问题 | 方案 | 结论 |
|---|---|---|
| 在哪一层录制 | HTTP 层（类似 vcrpy） | 不采用：请求里带每次都不同的 start/end 时间戳，匹配要做很多特殊处理；cassette 也难读 |
| | **工具层：(工具, 参数) → 原始结果** | **采用**：与传输无关，cassette 可读；改了摘要逻辑也能用老录音重新验证 |
| | 摘要层：直接录摘要 | 不采用：摘要逻辑一改，录音就作废；证据里也就没有原始数据 |
| 证据 ID | 随机 | 不采用：回放时 transcript 变了，LLM 回放对不上；重试会产生重复证据 |
| | **由内容派生** | **采用** |
| 访问 Kubernetes | kubectl 子进程 | 不采用：worker 镜像要装 kubectl，权限跟着 kubeconfig 走，难以收窄 |
| | kubernetes Python 客户端 | 不采用：依赖很重，用到的只是 3 个 GET |
| | **httpx + ServiceAccount token** | **采用**：和集群内运行时的认证方式相同 |
| 工具怎么暴露给 Agent | MCP server | 暂不采用：本项目的 Agent 循环是自己写的，函数调用最直接；把同一批工具包装成 MCP server 是加分项 |
| | **注册表 + 函数调用** | **采用** |

## 后果

- 正面：
  - Agent 的每条结论都能落到一行不可变、带哈希的证据上，M4 的引用校验器可以确定性地检查"这个 ID 存在、属于这个事件"。
  - 工具层在 CI 里完全确定：录音 + 回放，不需要集群，也不花额度。
  - 错误信息对模型是可行动的，查询写错可以自我修正，而不是让整个调查失败。
- 代价：
  - 原始结果全部入库，链路查询每次几百 KB。实验规模下没问题；量大以后要把原始结果移到对象存储，数据库里只留哈希和位置。
  - 摘要会丢信息。缓解：摘要里保留计数、Top-N 和"是否被截断"，Agent 可以缩小范围再查。
  - 证据行不可变，按事件清理旧数据时要在运维脚本里显式绕过触发器，这是有意的摩擦。
  - worker 跑在 docker compose 里，访问 kind 里的服务还需要接入 kind 的 Docker 网络。M3 的工具都从宿主机调用，这个问题留给 M4 解决。
- 什么情况下应该重新考虑：
  - 需要把工具开放给其他客户端（IDE、别的 Agent）：包装成 MCP server。
  - 后端换成托管服务（如 Grafana Cloud、Datadog）：只需重写 `fetch`，摘要和录制回放不变。
