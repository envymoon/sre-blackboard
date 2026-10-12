# ADR-0003：用 Postgres `SKIP LOCKED` 实现任务队列

- 状态：已接受
- 日期：2026-10-12
- 相关里程碑：M1

## 背景

原型的队列（`app/queue.py`）是进程内的列表，有两个 bug：

- **B1：** 只要来过一个 P0，`p0_arrived` 就永远为真，P1–P3 再也不会被派发；
- **B2：** 租约过期后只记录了任务 ID 和次数，优先级丢了，任务也不会被真正放回队列。

此外，它没有心跳、没有 fencing，worker 的 `main` 只打印一行字（B8）。

后续里程碑里，一个任务可能要跑几分钟（多轮 LLM 调用），worker 随时可能被重启或卡住。队列必须保证：

1. 同一个任务不会同时被两个 worker 执行；
2. worker 死掉后，任务能被别人接手；
3. 卡住又"复活"的 worker 不能把过期的结果写回去；
4. 高优先级优先，但低优先级不会饿死。

## 决定

**表：** `jobs`。关键列是 `status`（queued / running / done / dead）、`priority`、`sort_key`、`run_after`、`attempts`、`max_attempts`、`lease_owner`、`lease_until`、`fence`。

**领取：**

```sql
WITH next AS (
  SELECT id FROM jobs
  WHERE status = 'queued' AND run_after <= now() AND priority <= :max_priority
  ORDER BY sort_key, id LIMIT 1
  FOR UPDATE SKIP LOCKED
)
UPDATE jobs SET status = 'running', lease_until = now() + :ttl,
                fence = fence + 1, attempts = attempts + 1, ...
FROM next WHERE jobs.id = next.id RETURNING jobs.*;
```

`SKIP LOCKED` 让并发的 worker 各自锁住不同的行，不会排队等同一行，也不会领到同一个任务。

**优先级老化（修复 B1）：** `sort_key = 入队时间 + priority × aging_step`，默认步长 60 秒，入队时一次算好。

- 同一步长内，P0 先于 P3；
- 一个 P3 只要等了超过 3 个步长，就排在所有新到的 P0 前面；
- 所以 P3 的最长等待时间有上界：3 个步长，加上在它之前入队的任务处理完所需的时间；
- `sort_key` 是普通列，可以直接建部分索引（`WHERE status = 'queued'`）。如果在查询时计算 `now() - enqueued_at`，就用不上索引。

**为 P0 预留槽位：** 每个 worker 有 `concurrency` 个并发槽位，其中最后 `reserved_p0_slots` 个只领取 P0（`max_priority = 0`）。worker 再忙，新来的 P0 也不用排队等槽位，而且其他优先级仍然在正常处理。原型的做法是 P0 一来就停掉其他所有优先级，这正是 B1 的原因。

**租约和回收（修复 B2）：**

- 处理任务期间，worker 每 `ttl / 3` 续约一次。
- 回收任务（每个 worker 都会周期性运行，靠 `SKIP LOCKED` 保证互不干扰）把 `lease_until` 已过期的任务改回 `queued`；`attempts` 已用完的改成 `dead`。
- 回收只改状态和租约字段，**不动 `priority` 和 `sort_key`**，所以任务保留优先级，也保留原来在队列中的位置。

**fencing token：**

- 每次领取，`fence + 1`。
- worker 的每一次写入都带着自己领到的 fence。完成、失败、续约，以及处理器里的业务写入（经由 `JobContext.transaction()`）都是这样。
- 写入前先 `SELECT … WHERE id = :id AND fence = :mine FOR UPDATE`；fence 对不上就抛 `LeaseLost`，整个事务回滚。
- 续约时如果发现 fence 已经变了，worker 立即取消正在运行的处理器。

**重试和死信：**

- 处理器抛出普通异常：按指数退避加抖动设置 `run_after`，重新排队。
- 次数用完，或者抛出 `PermanentJobError`：进入 `dead`，同时写一条审计。

**投递语义：至少一次。**

- 处理器必须幂等。例如 triage 处理器发现事件已经不是 RECEIVED 状态，就直接返回。
- 状态变更用乐观锁（`version`）。
- 后续任务用 `idempotency_key` 入队，重复入队不会产生新任务。
- 至少一次投递加上幂等处理，效果上等于恰好一次。

**优雅退出（修复 B8）：**

- 收到 SIGTERM 后，worker 停止领取新任务，等待正在运行的任务完成，最多等 `shutdown_grace_s`。
- 超时还没完成的任务，worker 会取消它，并交还给队列：`release` 不计入尝试次数，因为这不是任务本身的错。

## 考虑过的方案

| 方案 | 优点 | 缺点 | 结论 |
|---|---|---|---|
| Redis Streams / List | 延迟低，吞吐高 | 多一个组件；入队不能和建事件放在同一事务里，需要 outbox；租约和 fencing 仍要自己实现 | 不采用 |
| Celery | 生态成熟 | 语义不透明（ack 时机、可见性超时）；默认依赖 Redis 或 RabbitMQ；面试时很难讲清它在出错时的行为 | 不采用 |
| Temporal | 持久化工作流，重试、超时、人工信号都是内置的 | 需要单独部署一套集群，对这个项目太重；学习曲线陡 | 不采用。作为规模扩大后的演进方向 |
| Postgres `SKIP LOCKED` | 和业务数据同一事务；每个机制都能用几十行 SQL 讲清楚；测试只需要一个 Postgres | 轮询带来延迟（默认 0.5 秒）；吞吐有上限 | **采用** |

## 后果

- 正面：
  - 三类关键行为都有测试：多个 worker 并发领取 200 个任务不重复；真实进程被 `kill -9` 后，任务在租约过期后被另一个 worker 接手；僵尸 worker 的写入被 fence 拒绝。
  - 压测：2 个 API 进程、2 个 worker，2000 条告警（200 个不同指纹），没有任何任务被领取两次（最大 fence 为 1）。
- 代价：
  - 空闲时每个 worker 每 0.5 秒查询一次。可以用 `LISTEN/NOTIFY` 唤醒来降低延迟，目前没有必要；
  - `jobs` 表会持续增长，需要定期归档已完成的任务；更新频繁的表要关注 autovacuum；
  - 至少一次投递把复杂度转移给了处理器：每个处理器都必须幂等，代码评审时要专门检查这一点。
- 什么情况下应该重新考虑：
  - 吞吐需要每秒几千个任务以上；
  - 工作流变得很长、有大量人工等待和定时器。这时候 Temporal 这类持久化工作流引擎的价值会超过它的部署成本。
