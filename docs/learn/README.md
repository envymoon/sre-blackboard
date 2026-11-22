# 学习笔记

每个里程碑一篇，结构相同：这一阶段做了什么 → 背景知识 → 本项目怎么做 → 踩过的坑 → 验收结果 → 面试问答。数字和结论都能在仓库里找到出处，汇总在 [`../results.md`](../results.md)。

想按系统组件而不是按里程碑读，看 [《SRE Blackboard 设计全书》](../book/README.md)：16 章，带配图和面试官追问。

| 笔记 | 主题 | 问答 |
|---|---|---|
| [M0](m0-engineering-baseline.md) | 诚实化与工程基线：uv、ruff、mypy、CI、fail fast | 8 |
| [M1](m1-postgres-queue.md) | Postgres 任务队列（`SKIP LOCKED`、租约、fencing、老化）、无状态 API、鉴权 | 10 |
| [M2](m2-fault-lab.md) | 故障实验环境：kind、OpenTelemetry Demo、SLO 燃烧率告警、故障注入 | 8 |
| [M3](m3-tools.md) | 只读工具、证据快照、录制回放、结果压缩 | 10 |
| [M4](m4-agent.md) | 手写 Agent 循环、可插拔 LLM 后端、黑板、引用校验 | 10 |
| [M5](m5-safety.md) | 策略引擎、绑定哈希的审批、独立执行器、SLO 验证、提示注入 | 9 |
| [M6](m6-rag.md) | Postgres 混合检索、RRF、重排、检索评测 | 10 |
| [M7](m7-eval.md) | 人工标注的场景、并排消融、回放闸门、金丝雀 | 10 |
| [M8](m8-observability.md) | 一个事件一棵 trace、GenAI 语义约定、Prometheus 指标、SLO 告警 | 12 |
| [M9](m9-console.md) | React 控制台、SSE、设计 token、性能预算、真实事件回放 | 10 |

笔记按里程碑编号排列；实际执行顺序是 M0 → M1 → M2 → M3 → M4 → M5 → M9 → M7 → M6 → M8。

## 时间不多时先读哪些

1. **整体**：[架构图](../architecture.md)，再看 README 开头的[演示动图](../assets/demo-bad-release.gif)，它是一次真实事件从告警到恢复的回放。
2. **最能说明"不是玩具"的三处**：
   - M4 第 4 节：真实模型在真实故障上的结果，包括判错的那次；
   - M5 第 4 节：坏版本发布的完整闭环，审计链可校验；
   - M7 第 4 节：并排消融，以及闸门拦下故意写坏的提示。
3. **最常被追问的设计**：
   - M1 Q1–Q4：为什么用 Postgres 做队列、fencing token、"恰好一次"；
   - M4 的引用校验：模型引用的证据 ID 不用模型就能校验；
   - M5 的"模型只能提议"：策略不读模型写的文字，执行器单独一个账号。
4. **诚实的边界**：[`../results.md`](../results.md) 最后一节列出了还没有证明的事。
