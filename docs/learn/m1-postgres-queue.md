# M1 学习笔记：Postgres、任务队列、无状态 API 与鉴权

> 对应里程碑：M1 · 相关决策：[ADR-0002](../adr/0002-single-postgres.md)、[ADR-0003](../adr/0003-postgres-skip-locked-queue.md)
> 这一阶段把"进程内的玩具状态"换成了数据库里的真实状态。它的价值不在功能多，而在出错时的行为是确定的：重复请求、并发领取、worker 被杀、worker 假死后复活，每一种情况都有测试证明系统仍然正确。

## 1. 这一阶段做了什么

| 类别 | 改动 | 修复 |
|---|---|---|
| 存储 | Postgres 16 + Alembic；`incidents`、`alerts`、`jobs`、`audit_log` 四张表；SQLAlchemy Core + psycopg 3（异步） | T9、B13 |
| 告警接入 | 兼容 Alertmanager webhook；部分唯一索引实现幂等；告警、事件、任务、审计在同一个事务里写入 | B3 |
| 队列 | `SKIP LOCKED` 领取、优先级老化、P0 预留槽位、租约续约与回收、fencing、指数退避、死信 | B1、B2 |
| Worker | 真正的主循环；心跳；租约丢失时取消处理器；SIGTERM 优雅退出 | B8 |
| 状态机 | 显式的状态转换表；非法转换抛异常；乐观锁（`version`）；每次转换写审计 | — |
| 审计 | 完整 SHA-256 哈希链；触发器禁止 UPDATE/DELETE；`verify` 能定位第一处被篡改的行 | B9（M5 补 CLI 和接口） |
| 鉴权 | JWT（HS256）+ 角色；租户只来自 token；CORS 白名单 | B4、B5、B6 |
| 启动 | 签名密钥缺失或太短、数据库连不上，进程都直接退出；`/healthz` 和 `/readyz` 分开 | — |

## 2. 背景知识

### 2.1 `FOR UPDATE SKIP LOCKED`：为什么数据库可以当队列

普通的 `SELECT … FOR UPDATE` 会锁住选中的行；另一个事务想锁同一行，就得排队等待。如果十个 worker 同时去领"排在最前面的任务"，它们会排成一队，九个在等。

`SKIP LOCKED` 的意思是：遇到已经被别人锁住的行，直接跳过，去看下一行。于是十个 worker 会各自锁住十个不同的任务，互不等待，也不会领到同一个。

领取用一条语句完成"选出 + 标记为 running"：

```sql
WITH next AS (SELECT id FROM jobs WHERE status = 'queued' ... LIMIT 1 FOR UPDATE SKIP LOCKED)
UPDATE jobs SET status = 'running', fence = fence + 1, ... FROM next WHERE jobs.id = next.id RETURNING *;
```

在同一条语句里完成很重要。如果先 SELECT 再 UPDATE、而且不加锁，两个 worker 可能同时选中同一行。

### 2.2 租约、心跳与 fencing token

- **租约（lease）**：领到任务的 worker 只"借用"它一段时间（`lease_until`）。worker 死了不会续约，租约一过期，回收任务就把它放回队列。
- **心跳**：任务运行期间，worker 每 `ttl/3` 续约一次。用三分之一是为了容忍偶尔一两次续约失败。
- **为什么只有租约还不够**：考虑这个时间线。
  1. worker A 领到任务；
  2. A 因为 GC 停顿或网络分区卡住了 40 秒，租约过期；
  3. 回收任务把任务放回队列，worker B 领到并开始执行；
  4. A 恢复，它不知道自己已经失去租约，接着写结果。

  这时 A 和 B 都在写，这就是"僵尸 worker"问题。
- **fencing token**：每次领取，`fence + 1`。A 领到时是 1，B 领到时是 2。所有写入都带着自己的 fence，并在同一个事务里先检查 `fence = :mine`。A 带着 1 去写，检查失败，事务回滚。关键在于**检查和写入由存储层在同一事务里完成**，而不是 worker 自己判断"我还有租约吗"，因为卡住的 worker 恰恰无法正确判断这一点。
- 这个概念来自 Martin Kleppmann 对 Redlock 的批评文章和《数据密集型应用系统设计》第 8 章。

### 2.3 至少一次 + 幂等 = 效果上的恰好一次

- 分布式系统里，真正的"恰好一次投递"基本做不到：worker 可能在写完结果之后、标记 done 之前崩溃，任务就会再跑一次。
- 实际做法是**接受至少一次，让处理变得幂等**：
  - triage 处理器先看事件状态，已经不是 RECEIVED 就直接返回；
  - 状态变更带乐观锁：`UPDATE … WHERE version = :v`，重复执行的那次会因为 version 不匹配而失败；
  - 后续任务用 `idempotency_key` 入队（唯一约束），重复入队不会产生第二个任务。
- 这三层合起来，任务即使执行两次，效果也和执行一次一样。

### 2.4 优先级与饥饿

- **严格优先级**（P0 永远先于 P3）在持续有 P0 到达时，P3 会永远等下去，这叫饥饿。原型更极端：来过一次 P0，就永远不派发 P1–P3（B1）。
- **老化（aging）**：等得越久，有效优先级越高。这里的实现是入队时计算 `sort_key = 入队时间 + priority × 60 秒`，按 `sort_key` 排序。这等价于"每个优先级级别相当于晚到 60 秒"，所以一个 P3 等够 3 分钟，就比新到的 P0 更靠前。
- 为什么在入队时算好，而不是在查询时计算 `now() - enqueued_at`：前者是一个普通列，可以建索引；后者每次查询都要对所有排队任务重新计算。
- **预留槽位**解决另一个问题：worker 的并发槽位全被 P2 占满时，新来的 P0 要等某个 P2 跑完。所以每个 worker 的最后一个槽位只领 P0。

### 2.5 幂等接入：部分唯一索引 + `ON CONFLICT`

```sql
CREATE UNIQUE INDEX ux_incidents_open_fingerprint ON incidents (tenant_id, fingerprint) WHERE closed_at IS NULL;

INSERT INTO incidents (...) VALUES (...)
ON CONFLICT (tenant_id, fingerprint) WHERE closed_at IS NULL DO NOTHING
RETURNING id;
```

- 50 个并发请求同时插入同一个指纹，只有一个能成功。其余的会等第一个事务提交，然后什么都不做（`DO NOTHING`），再由 `UPDATE … alert_count + 1` 把告警合并进已有事件。
- 为什么不按时间窗口去重：10 分钟分桶时，09:59:59 和 10:00:00 的两条告警会落进两个桶，建出两个事件。"同一指纹同时只能有一个未关闭的事件"没有这个边界问题。
- "先查有没有，没有再插入"（check-then-act）在并发下一定会出错：两个请求都查到"没有"，然后都插入。唯一约束是唯一可靠的做法。

### 2.6 事务内入队：为什么不需要 outbox

如果队列在 Redis、事件在数据库，"建事件"和"入队"就是两次写入：

- 先写库后入队：入队前崩溃，事件就没有任务；
- 先入队后写库：任务可能先跑起来，却找不到事件。

outbox 模式的做法是：把待发消息和业务数据写进同一个事务里的 outbox 表，再由另一个进程搬运到消息队列。这个项目的队列本身就是同一个库里的一张表，入队就是一条 INSERT，天然和业务写入在同一个事务里。

### 2.7 哈希链审计

- 每行 `hash = sha256(上一行的 hash + 本行内容的规范化 JSON)`。改任意一行，这一行的 hash 就对不上；删一行，下一行的 `prev_hash` 就对不上。
- **两层防护：**
  - 数据库触发器禁止 UPDATE 和 DELETE，防止应用 bug 修改审计记录；
  - 哈希链能发现有权限关掉触发器的人做的篡改。测试里模拟了这种情况：关掉触发器、修改数据、再打开，`verify` 仍然能报出被改的行。
- **并发：** 两个事务同时追加，可能都读到同一个"最后一行"，链就分叉了。用 `pg_advisory_xact_lock` 把追加串行化。这把锁在事务提交时自动释放，所以后一个事务一定能看到前一个事务写入的行。
- **规范化：** JSONB 不保留键的顺序，所以计算哈希时用 `sort_keys=True`；时间统一转成 UTC 的 ISO 格式。否则同样的数据读回来，算出的哈希会不同。

### 2.8 JWT 的几个坑

- **固定算法**：`jwt.decode(..., algorithms=["HS256"])`。如果按 token 头部的 `alg` 来选算法，攻击者可以发一个 `alg: none` 的 token，或者利用 RS256/HS256 混淆攻击。测试里专门覆盖了 `alg: none`。
- **校验 `aud` 和 `iss`**：防止别的系统签发的 token 被拿来用。
- **租户只来自 token**：请求体里的任何字段都不能决定租户。测试里告警标签带着 `tenant_id=t2`，事件仍然属于 token 里的 t1。缺少租户声明的 token 直接 401（fail closed，修复 B6）。
- **跨租户访问返回 404，而不是 403**：403 会泄露"这个 ID 存在，只是不属于你"。
- **密钥太短就拒绝启动**：HS256 的安全性完全取决于密钥。

### 2.9 启动、探针与退出

- **liveness（`/healthz`）只检查进程本身，不查数据库。** 如果 liveness 依赖数据库，数据库一抖动，Kubernetes 会把所有 API 进程一起重启，让故障扩大。
- **readiness（`/readyz`）检查数据库。** 数据库不可用时返回 503，负载均衡器暂时不把流量发过来，但不会重启进程。
- **启动时连一次数据库**：连不上就直接退出（fail fast），而不是启动后每个请求都报错。
- **SIGTERM**：Kubernetes 和 Docker 停止容器时先发 SIGTERM，等待一段宽限期，再发 SIGKILL。worker 在宽限期内停止领取、等手上的任务完成；完不成的交还队列，不计入失败次数。compose 里 `stop_grace_period: 30s` 要大于 worker 的 `shutdown_grace_s`（20 秒）。

## 3. 踩过的坑，以及设计时预先规避的坑

| 现象 | 原因 | 解决 |
|---|---|---|
| 用 httpx 的 `ASGITransport` 测 API 时，`app.state.engine` 不存在 | `ASGITransport` 不会触发 FastAPI 的 lifespan（启动和关闭钩子） | 测试里用 `async with app.router.lifespan_context(app)` 手动进入 lifespan |
| Alertmanager 发来的告警 `endsAt` 是 `0001-01-01T00:00:00Z` | 告警还在触发时，Alertmanager 用"零值时间"表示没有结束时间 | 只有 `status=resolved` 时才保存 `ends_at` |
| （预先规避）两个并发 webhook 包含相同的两组告警，可能死锁 | 事务 A 先锁指纹 X 再锁 Y，事务 B 顺序相反 | 一个 webhook 里的告警先按指纹排序再处理，所有事务按同一顺序加锁 |
| （预先规避）直接对读回的数据计算审计哈希，会和写入时算出的不一致 | JSONB 会重新排列键的顺序；时间读回来时时区表示可能不同 | 规范化：`sort_keys=True`，时间统一转成 UTC |
| 云端开发容器里 testcontainers 启动失败 | Docker Hub 限流（429）；testcontainers 的 Ryuk 清理容器也在 Docker Hub 上 | 会话启动脚本从 `mirror.gcr.io` 拉 Postgres 镜像并重新打标签，同时设置 `TESTCONTAINERS_RYUK_DISABLED=true` |
| `uv run python scripts/loadtest.py` 报 `No module named 'app'` | 直接运行脚本时，`sys.path` 里是脚本所在目录，而不是仓库根目录 | 改成 `python -m scripts.loadtest`，以模块方式运行 |

## 4. 验收结果

本地（云端开发容器）运行：2 个 API 进程（`uvicorn --workers 2`）、2 个 worker 进程、1 个 Postgres。

| 场景 | 结果 |
|---|---|
| 1000 条告警，10 个不同指纹，并发 50 | 0 失败；10 个事件，告警数合计 1000；10 个任务全部完成，最大 fence 为 1；10 个事件全部 TRIAGED；审计链校验通过 |
| 2000 条告警，200 个不同指纹，并发 100 | 0 失败；200 个事件；200 个任务全部完成，最大 fence 为 1；两个 worker 各处理约一半；审计链校验通过 |
| 同上，跑在 `docker compose` 全栈里（2026-10-14） | 全部检查通过，约 230 条/秒；`docker compose down` 1.5 秒内完成，worker 优雅退出 |

CI 的 `e2e` job 在每个 PR 上用原样的 Dockerfile 重跑 compose 验收，所以这条路径不依赖本地机器。

## 5. 面试问答

**Q1：为什么用 Postgres 做队列，而不是 Redis 或 Kafka？**
答：主要是事务边界。建事件和派任务必须同时成功或同时失败。队列在 Redis 里，就要引入 outbox 才能保证这一点。队列在同一个 Postgres 里，入队就是同一事务里的一条 INSERT。代价是吞吐上限和轮询延迟。对每天几千条告警的规模，这些都不是问题。到了每秒几千个任务的规模，我会考虑换掉它。

**Q2：`SKIP LOCKED` 解决了什么问题？不加会怎样？**
答：不加的话，所有 worker 会争抢排在最前面的同一行。一个 worker 拿到锁，其他的排队等待，并发度退化成 1。加了之后，每个 worker 跳过别人锁住的行，各自拿到不同的任务。

**Q3：有了租约，为什么还需要 fencing token？**
答：租约只能保证"过期后别人可以接手"，不能阻止原来的持有者在过期之后继续写。一个 GC 停顿了 40 秒的 worker 醒来时并不知道自己已经失去租约。fencing token 把检查放在存储层：每次领取时 token 加一，写入时在同一个事务里校验 token。过期持有者手上的 token 是旧的，写入一定失败。我有一个测试专门模拟这个过程。

**Q4：你的队列是"恰好一次"吗？**
答：投递是至少一次，因为 worker 可能在完成工作之后、标记 done 之前崩溃。效果上做到恰好一次，靠的是处理器幂等：状态检查、乐观锁，以及带唯一键的后续入队。这是行业里的标准做法。声称"恰好一次投递"的系统，内部也是这样实现的。

**Q5：优先级队列怎么避免饥饿？**
答：用老化。排序键是"入队时间加优先级乘以 60 秒"，等得足够久的低优先级任务会排到新来的高优先级前面，所以等待时间有上界。另外每个 worker 为 P0 预留一个槽位，保证 P0 不会因为槽位被低优先级占满而等待。原型的问题是"来过一次 P0 就停掉其他所有优先级"，这是最坏的饥饿。

**Q6：告警去重怎么保证在多实例、高并发下正确？**
答：靠数据库的唯一约束，不靠应用代码先查再插。我用的是部分唯一索引：同一租户、同一指纹，只能有一个未关闭的事件。写入用 `INSERT ON CONFLICT DO NOTHING`，冲突时合并。我有一个测试同时发 50 个相同的告警，结果只有一个事件，告警数是 50。我没有用按时间分桶的窗口，因为分桶在边界上会把同一次故障拆成两个事件。

**Q7：worker 收到 SIGTERM 时做了什么？**
答：停止领取新任务，等正在运行的任务完成，最多等宽限期。宽限期结束还没完成的任务，worker 主动取消并交还队列，这次不计入尝试次数，因为这不是任务本身的错。如果 worker 是被 `kill -9` 杀掉的，没有机会交还，就靠租约过期后的回收。两种情况都有用真实子进程跑的测试。

**Q8：多租户隔离是怎么做的？**
答：租户只来自经过签名校验的 token，请求体里的字段一律不信。没有租户声明的 token 直接拒绝。所有查询都带上 token 中的租户条件；访问别的租户的事件返回 404，避免泄露 ID 是否存在。更进一步，可以用 Postgres 的行级安全策略（RLS）在数据库层再加一道，作为纵深防御，这是后续可以做的。

**Q9：审计日志怎么防篡改？**
答：分两层。数据库触发器禁止修改和删除，防的是应用 bug。哈希链防的是有权限关掉触发器的人：每行的哈希包含上一行的哈希，改任何一行，从这一行开始的整条链都会对不上，`verify` 能报出第一处被改的位置。追加时用 advisory lock 串行化，防止并发追加让链分叉。

**Q10：liveness 和 readiness 探针有什么区别？为什么 liveness 不检查数据库？**
答：liveness 失败会导致重启进程，readiness 失败只会让流量暂时不发过来。如果 liveness 检查数据库，数据库一抖动，所有 API 实例会被一起重启，小故障变成大故障。所以数据库检查只放在 readiness 里。
