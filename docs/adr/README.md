# 架构决策记录（ADR）

每篇记录一个关键选型：当时的背景和约束、做了什么决定、放弃了哪些方案、带来了什么后果。新写的从 [`0000-template.md`](0000-template.md) 开始，编号按写成的顺序分配。

| 编号 | 决定 | 里程碑 |
|---|---|---|
| [0001](0001-depth-over-breadth.md) | 深度优先，只做一条真实闭环 | M0 |
| [0002](0002-single-postgres.md) | 用一个 Postgres 承担状态、队列和检索 | M0 |
| [0003](0003-postgres-skip-locked-queue.md) | 用 Postgres `SKIP LOCKED` 实现任务队列 | M1 |
| [0004](0004-fault-lab-on-kind-in-ci.md) | 故障实验环境用 kind + OpenTelemetry Demo，在 GitHub Actions 上运行 | M2 |
| [0005](0005-tool-layer-evidence-and-replay.md) | 工具层只读、结果存证据快照、按"调用 → 原始结果"录制回放 | M3 |
| [0006](0006-agent-loop-llm-backends-blackboard.md) | 手写 Agent 循环、可插拔 LLM 后端、黑板检查点与引用校验 | M4 |
| [0007](0007-remediation-policy-approval-executor.md) | 模型提议、确定性策略、绑定计划哈希的人工审批、独立执行器与 SLO 验证 | M5 |
| [0008](0008-console-sse-replay-design-tokens.md) | 控制台一套页面两种数据源；SSE 只通知"什么变了"；设计 token 和性能预算进 CI | M9 |
| [0009](0009-evaluation-scenarios-variants-gate.md) | 人工标注的场景、并排运行的 Agent 变体、回放闸门、必须失败的金丝雀 | M7 |
| [0010](0010-knowledge-base-hybrid-retrieval.md) | Postgres 混合检索、本地 CPU 模型、按小节标注的检索评测，知识只能作背景引用 | M6 |
| [0011](0011-system-observability.md) | 一个事件一棵 trace、Prometheus 指标、只告警给人的 SLO 规则 | M8 |

## 计划里的初稿题目去了哪里

重建计划附录 A 列了 14 个题目。写的时候几个题目合并进了同一篇，因为它们是同一个决定的不同侧面：

| 初稿题目 | 写在 |
|---|---|
| 深度优先：只做一条真实闭环 | 0001 |
| 用一个 Postgres 替代 MySQL + Redis + Elasticsearch | 0002 |
| 用 Postgres SKIP LOCKED 做任务队列 | 0003 |
| 手写 Agent 循环，不使用 LangGraph | 0006 |
| 黑板模式与结构化 Finding，而不是 Agent 之间对话 | 0006 |
| 证据快照与引用校验（防幻觉） | 0005（快照）、0006（校验） |
| AI 只读 + 风险分级 + 人工审批 + 独立执行器 | 0007 |
| 提示注入的纵深防御 | 0007 |
| 用 OTel Demo + 可控故障注入做评测环境 | 0004、0009 |
| 录制/回放：让 LLM 系统可测试 | 0005（工具）、0006（模型） |
| 混合检索与 RRF | 0010 |
| 模型分级与成本预算 | 0006（路由和预算）、0009（变体对比） |
| 可插拔 LLM 后端：用 `claude -p` 订阅额度做单步推理引擎 | 0006 |
| 前端设计系统与性能预算 | 0008 |
| （初稿没有）系统自身的可观测性 | 0011 |
