# M0 学习笔记：诚实化与工程基线

> 对应里程碑：M0 · 相关决策：[ADR-0001](../adr/0001-depth-over-breadth.md)
> 这一阶段几乎没有新功能，做的是"让仓库值得信任"：说的和做的一致，任何人 clone 下来一条命令就能跑通检查。

## 1. 这一阶段做了什么

| 类别 | 改动 |
|---|---|
| 诚实化 | README 重写为只描述已实现的内容；删除虚构的模型名和价格表；删除对不公开规格文档的 `Sec.x` 引用；修正"生产环境用 MySQL"之类与事实不符的注释；GitHub Pages 加上"脚本演示，非真实运行"的横幅 |
| 删除空壳 | 9 个只有常量或伪造行为、没有被任何流程使用的模块，12 个占位脚本，以及对应的"验证常量"测试（理由见 ADR-0001） |
| 下线不安全的接口 | 无鉴权的审批接口、返回写死数据的事件详情和报告接口、`CORS *` |
| 修复 bug B7 | 应用初始化时任何异常都会被吞掉并让 `app = None`，现在启动失败会直接报错退出 |
| 配置 | `pydantic-settings`，环境变量前缀 `SRE_`；模型默认 `claude-haiku-5-5`，可以按角色覆盖 |
| 工程化 | `pyproject.toml` + `uv.lock`、ruff（lint + format）、mypy、pytest、pre-commit、GitHub Actions、Makefile、基于 uv 的 Dockerfile |

## 2. 背景知识

### 2.1 依赖管理：为什么用 uv 和锁文件

- `requirements.txt` 里写 `fastapi`，不带版本号，等于"安装那一刻的最新版"。今天能跑，三个月后可能就跑不了，这叫**不可复现构建**。
- **锁文件**（`uv.lock`）记录了整个依赖树中每个包的确切版本和哈希。任何人、任何时间、在 CI 和 Docker 里，安装的都是同一组包。
- `pyproject.toml` 写"我需要什么"（版本范围，如 `fastapi>=0.115`），`uv.lock` 写"实际装了什么"（精确版本）。两者分工明确。
- CI 和 Docker 中使用 `uv sync --locked`：如果 `pyproject.toml` 改了但没有重新生成锁文件，就直接失败，不会悄悄装上别的版本。
- **依赖分组**：`[dependency-groups] dev` 放 pytest、ruff、mypy 这类开发工具。生产镜像用 `--no-dev`，体积更小，攻击面也更小。
- 为什么选 uv 而不是 pip-tools 或 Poetry：uv 速度快一个数量级，单一二进制，同时管理 Python 版本、虚拟环境和锁文件。

### 2.2 静态检查：ruff 和 mypy

- **ruff** 同时承担 linter 和 formatter（替代 flake8、isort、black 等多个工具）。本项目开启的规则集：
  - `E/W`：pycodestyle 风格；
  - `F`：pyflakes，未使用的导入、未定义的名字；
  - `I`：import 排序；
  - `B`：bugbear，常见 bug 模式，如可变默认参数、未使用的循环变量；
  - `UP`：升级到新语法，如 `list[str]` 代替 `List[str]`；
  - `SIM`：可简化的写法；
  - `RUF`：ruff 自有规则。
- 关闭了 `E501`（行太长）：格式化器已经负责把代码折行，剩下的超长行基本是字符串字面量，强行拆开反而降低可读性。
- **mypy 渐进式类型检查**：对一个已有代码库直接开 `--strict`，会报几百个错误。策略是先开 `check_untyped_defs`（检查函数体内部），随着每个模块在 M1–M6 中被重写，再逐个模块收紧。这是业界给老代码加类型的标准做法。

### 2.3 CI 设计

- CI 运行的命令和本地 `make check` **完全一致**。"本地通过、CI 失败"大多是因为两边跑的东西不一样。
- 步骤按"便宜、快"到"贵、慢"排序：lint → format → 类型检查 → 测试。越早失败，反馈越快。
- uv 的版本也固定（`setup-uv` 的 `version`），避免工具本身升级导致的行为变化。
- `permissions: contents: read`：CI 默认给 `GITHUB_TOKEN` 最小权限，这是供应链安全的基本要求。

### 2.4 pre-commit

- 在 `git commit` 时自动运行检查，把问题挡在提交之前。
- 本项目的 ruff 和 mypy 钩子用 `language: system` 加 `uv run`，直接复用锁文件中的版本，避免"pre-commit 里的 ruff 和项目里的 ruff 版本不一致"这个常见问题。

### 2.5 Docker 镜像

```dockerfile
COPY --from=ghcr.io/astral-sh/uv:0.11.32 /uv /bin/uv   # 从官方镜像复制 uv 二进制，版本固定
COPY pyproject.toml uv.lock ./                           # 先只复制依赖描述文件
RUN uv sync --locked --no-dev                            # 依赖层：只要锁文件不变，这一层就命中缓存
COPY app/ app/                                           # 代码层：改代码只会重建这一层
```

- **层缓存**：Docker 按指令逐层缓存。依赖变化少，代码变化多，所以先装依赖、后拷代码，日常改代码时重新构建只需要几秒。
- **单 worker**：入口的去重状态目前还在进程内存里，多个 worker 之间不共享，会导致重复建事件。M1 把状态移到 Postgres 之后，才能水平扩展。这是"先承认限制，再消除限制"，而不是假装支持多 worker。

### 2.6 Fail fast：为什么不能吞掉启动异常（bug B7）

原代码：

```python
try:
    from fastapi import FastAPI

    app = FastAPI(...)
    ...  # 注册所有路由
except Exception:
    app = None
```

问题：任何一个路由注册时出错，进程都会"正常启动"，但 `app` 是 `None`。uvicorn 报一个莫名其妙的错误，或者更糟，健康检查以外的接口全部失效，而真正的异常被吞掉了。

正确的做法是 **crash-only**：配置错误、依赖缺失时让进程以非零码退出，日志里留下真实的堆栈，由编排器（Docker、Kubernetes）负责重启和告警。"启动失败"是一个应该被立刻看见的信号，而不是被隐藏。

### 2.7 配置：12-factor 与 pydantic-settings

- 12-factor 原则：配置放在环境变量中，与代码分离，同一个镜像通过不同的环境变量部署到不同环境。
- `pydantic-settings` 会把环境变量解析成有类型的对象，类型不对时启动即报错（又一个 fail fast）。例如 `SRE_LLM_ROLE_MODELS='{"synthesizer":"claude-sonnet-5-5"}'` 会被解析成 `dict[str, str]`。
- `get_settings()` 用 `lru_cache` 做成单例；测试中通过 `get_settings.cache_clear()` 加 `monkeypatch.setenv` 注入不同的配置。

## 3. 踩过的坑

| 现象 | 原因 | 解决 |
|---|---|---|
| 改用 uv 后所有测试都报 `No module named 'app'` | 以前靠启动脚本设置 `PYTHONPATH`；uv 的虚拟环境里没有这个变量 | 在 `pyproject.toml` 的 pytest 配置里写 `pythonpath = ["."]`，让测试不依赖外部环境变量 |
| ruff 报了 90 个问题 | 原型代码从未运行过 linter | 安全的自动修复直接应用；"不安全修复"（如重命名未使用的循环变量、合并 if）逐个审阅 diff 后再接受 |
| 云端开发容器里 `docker build` 失败 | 守护进程默认没启动；Docker Hub 限流（429）；GHCR 的镜像下载域名被网络策略拦截；构建中的进程连不上宿主机代理，也不信任代理证书 | 手动启动 `dockerd`；基础镜像改从 `mirror.gcr.io` 拉；构建时加 `--network host`，并把代理证书和 `HTTPS_PROXY` 显式传进去。验证用的变体改用 pip 安装同版本 uv，原版 Dockerfile 在本地或 CI 构建 |
| 云端跑不起 Kubernetes（kind、k3s 都试过） | 沙箱禁止调低进程的 `oom_score_adj`（root 也不行），而 kubelet 创建每个 Pod 的 sandbox 容器时都要把它设成负值 | 这是沙箱限制，放开网络也解决不了。故障环境只在本地跑，云端用录制/回放开发 |
| 测试时出现 `StarletteDeprecationWarning`，提示改用 `httpx2` | 新版 Starlette 的 TestClient 准备切换底层 HTTP 客户端 | 暂时保留警告，不影响结果；等 FastAPI 官方给出迁移方式后再处理 |

## 4. 面试问答

**Q1：你的项目一开始写了很多功能，为什么后来删掉了一大半？**
答：第一版是照着设计文档搭的骨架，概念很全，但每个只有几十行，没有真实的模型调用，评测的期望值也是被测函数自己算出来的。我意识到，面试官挑任何一个点深挖都会落空。所以我做了一个取舍：先把一条闭环（注入故障 → 诊断 → 审批 → 回滚 → 验证）做成真的，再横向扩展。删掉的模块都在 git 历史里，有价值的想法记在 ADR 的"未来工作"里。

**Q2：`requirements.txt` 和锁文件有什么区别？**
答：前者通常只写直接依赖，而且常常不带版本，安装结果取决于安装的时间。锁文件记录了整棵依赖树的精确版本和哈希，保证任何时间、任何机器的安装结果都相同。CI 里用 `--locked`，锁文件和声明不一致时直接失败。

**Q3：为什么不一上来就开 mypy strict？**
答：对一个已有代码库直接开 strict 会产生大量错误，团队要么一次性停下来修，要么干脆关掉检查。渐进式的做法是先开基础检查，保证新代码不退化，再按模块逐步收紧。这个项目里的原型模块会陆续被重写，重写一个就收紧一个。

**Q4：Dockerfile 里为什么先 COPY 锁文件，再 COPY 代码？**
答：为了利用层缓存。依赖很少变化，代码经常变化。把依赖安装放在前面的层，改代码时这一层直接命中缓存，构建从几分钟降到几秒。

**Q5：应用启动时出错，应该捕获异常继续运行，还是直接退出？**
答：直接退出。配置错误这类问题，继续运行只会让服务处于"看起来活着、其实不能用"的状态，比宕机更难排查。让进程以非零码退出、留下堆栈，由 Kubernetes 重启并触发告警，问题能被立刻发现。这就是 fail fast，或者叫 crash-only 设计。

**Q6：为什么现在 API 只能跑一个 worker？这不是一个缺陷吗？**
答：是缺陷，而且我在 README 里如实写了出来。去重状态还在进程内存中，多个 worker 之间不共享，会重复创建事件。正确的做法是把幂等性交给数据库的唯一约束，这是 M1 的工作。在它完成之前，限制成单 worker，比假装支持水平扩展更负责。

**Q7：为什么删掉了审批接口，而不是先保留着？**
答：原来的审批接口没有鉴权，审批人由请求体自己填，CORS 还是 `*`，等于任何人都能批准一次生产回滚。一个不安全的接口比没有接口更糟。在 M1 加上鉴权、M5 实现完整的审批流程之前，选择不暴露，这是 fail-closed 的思路。

**Q8：CI 里为什么要固定 uv 的版本？**
答：工具本身也是依赖。不固定版本，某天工具升级改变了行为（比如解析规则），CI 就会无缘无故失败或者通过，很难排查。固定版本，升级成为一次显式的、可审查的变更。
