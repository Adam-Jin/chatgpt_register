## Context

`chatgpt_register.py`（3114 行）通过 `ThreadPoolExecutor`（默认 `max_workers=3`）并发跑账号注册流程，每个 worker 串联调用：

- `sentinel_solver.py` —— Cloudflare 验证码求解（异步事件循环跑在独立线程）
- DuckMail / `qq_mail_pool.py` —— 邮箱获取与 OTP 收件
- `sms_provider.py` + `herosms_pool.py` / `quackr_pool.py` —— 短信号申请 / 收码
- `phone_pool.py` —— HeroSMS 复用号租约管理（SQLite + lease + heartbeat）

当前所有日志使用 `print()` + `_print_lock`（`chatgpt_register.py:230`）串行写 stdout，多 worker 输出穿插难以分辨；子模块的 `log` 参数也都默认指向 `print`。

`phone_pool.acquire_or_reuse()`（`phone_pool.py:350`）的逻辑是「优先 `_try_claim_reused`，miss 后 fallback `provider.acquire()`」。`max_reuse`（默认 3）只约束**单个号被复用次数**，不约束**同时持有的号码总数**。当复用 miss 时（lease 抢占失败、`can_get_another=0`、`end_at` 安全余量不足、provider 状态延迟等），会直接拿新号；多 worker 同时 miss 会并发开多个新号，造成不必要的成本。

## Goals / Non-Goals

**Goals:**

- 把 worker、`sentinel`、`email`、`sms`、`phone_pool` 的日志按 channel 分开渲染，并保留一个全局合并视图
- 实时显示 `phone_pool` 申请累计 / 复用累计 / 复用率 / 当前活跃数 / 上限 / 花费
- 给 `phone_pool` 加并发持号上限，并在号码释放后动态补位；上限可配置，默认与 `max_workers` 对齐
- 非 TTY / `--no-tui` 场景平滑降级为带 channel + worker 前缀的纯文本日志
- 提供基础键盘交互：退出、暂停 / 恢复接新任务、切 worker 面板、切日志 filter
- 每个子系统的日志独立可滚动，能往回翻历史而不是只看 ring buffer 末尾

**Non-Goals:**

- 不持久化 TUI 状态（重启从 0 开始累计；历史看 `data.db` 与文件日志）
- 不重写子模块的核心业务逻辑，仅替换其 `log` 出口
- 不改 `data.db` schema（活跃数从现有字段计算）
- 不把 worker 主流程改成 async —— worker 继续跑在 `ThreadPoolExecutor`，仅 UI 是 async；通过事件桥隔离两个世界
- 不做账号详情 ModalScreen / 多 Tab 子页面 / 主题切换（首版克制范围，先把核心可见性做扎实）

## Decisions

### D1. Textual `App`

**选择**：使用 Textual 构建一个 `App`，由 `monitor.app.RegisterMonitorApp` 管理 widget 树和事件循环。Widget 选型：

- `Header` / `Footer`（Textual 内置，自动显示快捷键）
- 顶部 status bar：自定义 `Static` widget 显示聚合计数器，`reactive` 字段绑定数据
- Worker 区：横向 `Horizontal` 容器内放 N 个 `WorkerPanel`（自定义 `Static`，显示账号 / 步骤 / 计时 / 最近 8 行日志）
- Feature 区：每个 channel 一个 `RichLog` widget（原生支持滚动、复制、ANSI 着色）
- Pool Stats：自定义 `Static` widget，订阅 `pool.stats()` 的定时刷新
- All Logs：`TabbedContent` 套 4 个 `RichLog`（all / success / fail / warn），事件按 level 路由

**理由**：

- `RichLog` 解决了 Rich `Live` 方案最痛的点 —— 每个子系统的日志独立可滚动，能往回翻
- Textual 的 `reactive` + CSS 让聚合面板的更新和样式（颜色、边框、对齐）声明式表达，不用每次 rebuild Layout
- 键盘 / 鼠标交互（`Binding` 装饰器）几乎零成本，符合用户对 TUI 的直觉
- Textual 自己接管 stdout / stderr，避免和业务 print 打架

**代价**：

- Textual 是 async 框架（基于 `asyncio`），主程序入口要从 `executor.submit(...)` 改成 `app.run()` 后在内部 worker thread 里跑 `ThreadPoolExecutor` —— 详见 D2.5 桥接方案
- 包体积比 Rich 大；首次启动稍慢（< 200ms 量级，可接受）

**替代**：Rich `Live` —— 改动小、能用，但 RingBuffer 看不到历史、无法翻页、无交互；用户明确选 Textual。

### D2. 事件总线模型

**选择**：`monitor/bus.py` 提供单例 `EventBus`：

```python
@dataclass
class Event:
    ts: float
    channel: str          # "worker" | "sentinel" | "email" | "sms" | "phone_pool" | "system"
    worker_id: Optional[str]   # "W1".."Wn" 或 None（系统级）
    level: str            # "info" | "warn" | "error" | "success"
    msg: str
    fields: dict          # 结构化扩展（step, account, provider, ...）

class EventBus:
    def emit(self, channel, msg, *, worker_id=None, level="info", **fields): ...
    def subscribe(self) -> queue.Queue: ...   # 渲染线程消费
```

`bus.emit()` 是非阻塞的（队列满时 drop 并 inc 计数器，避免反压拖慢 worker）。

**理由**：业务代码改动最小 —— `print(f"...")` → `bus.emit("phone_pool", "...")`；channel 路由由 bus 决定，渲染层与业务解耦；非 TUI 模式 fallback 只需替换订阅者。

**替代**：Python `logging` 模块 —— 想过，但 `logging` 的 handler 模型在多线程下要做 channel 路由、结构化字段、TUI 集成都要自己写 `Handler`，等价于自己造一个总线，且与现有 `print` 出口不对齐，迁移成本反而高。保留 `logging` 兼容（bus 同时把事件转发给一个 `logging.getLogger("chatgpt_register")`），方便文件归档。

### D2.5. Sync ↔ Async Bridge（worker 线程 ↔ Textual 事件循环）

**选择**：

- 主入口改为：`app = RegisterMonitorApp(run_callable)`；`app.run()` 启动 Textual 事件循环
- `RegisterMonitorApp.on_mount` 中通过 `app.run_worker(self._kickoff_workers, thread=True)` 把 `ThreadPoolExecutor` 跑在 Textual 的 worker thread 里 —— 业务 worker 还是同步代码，对原有 `chatgpt_register.py` 流程零侵入
- `bus.emit()` 仍是线程安全的同步调用，写入 `queue.Queue`
- `RegisterMonitorApp` 用 `set_interval(0.1, self._drain_bus)`（运行在事件循环里）批量 pop 队列、按 channel/worker 路由调用 widget 的 `update()` / `RichLog.write()`
- Pool stats 用 `set_interval(1.0, self._refresh_pool_stats)` 在事件循环里调 `pool.stats()`（线程安全的同步调用）

**理由**：

- 业务侧（worker、子模块）保持纯同步，不需要 `asyncio` 改造
- UI 侧严格在事件循环里更新 widget —— 避免 Textual widget 跨线程访问的未定义行为
- 通过 `queue.Queue` + `set_interval` 这种"批 drain"模型，避免每条 emit 都触发一次 `call_from_thread`，渲染开销可控

**替代**：

- `app.call_from_thread(widget.update, ...)` 在每条 emit 调用 —— 高频日志会刷爆事件循环；放弃
- 把所有 worker 改成 async + `asyncio.to_thread` —— 改动面太大，影响子模块；放弃

### D3. Worker ID 注入

**选择**：`ThreadPoolExecutor` 默认 thread name 是 `ThreadPoolExecutor-0_N`，不直观。在提交任务时显式分配 `worker_id`（"W1".."Wn"），通过 `threading.local()` 暴露 `current_worker_id()`，bus.emit 默认从 thread-local 读，业务代码无需手传。

**理由**：避免在每个 `print` 调用点都加 `worker_id=...`；保证子模块（sentinel/email/sms/phone_pool）emit 的事件能自动归属到当前 worker 的面板。

**替代**：`contextvars` —— 同样可行；选 `threading.local` 因为现有代码全是线程模型，没有 async 上下文要传播。

### D4. 子模块日志出口迁移

**选择**：所有子模块（`phone_pool`、`sentinel_solver`、邮箱池、SMS 池）已经在构造函数接受 `log: Callable[[str], None]` 参数；`chatgpt_register.py` 把 `log=print` 改成 `log=bus.channel("phone_pool")` 这种 partial。子模块本身**零改动**。

`bus.channel(name)` 返回一个 `Callable[[str], None]`，把字符串作为 `msg` emit 到指定 channel。

**理由**：保持子模块对总线无依赖（仍可独立运行 / 测试 / 当库用）。

### D5. Phone Pool 并发持号上限

**选择**：在 `PhonePool` 内加 `max_active` + `threading.Condition`：

```python
class PhonePool:
    def __init__(self, ..., max_active: int = 0, acquire_timeout: float = 60.0):
        self.max_active = max_active     # 0 = 不限制（向后兼容）
        self.acquire_timeout = acquire_timeout
        self._cap_cond = threading.Condition()

    def _count_active(self, c) -> int:
        # status IN ('fresh','reused') AND (end_at IS NULL OR end_at > now)
        return c.execute("SELECT COUNT(*) FROM phone_pool WHERE ...").fetchone()[0]

    def acquire_or_reuse(self, **kw) -> PhoneLease:
        # 1) 先尝试复用（不占额外槽位 —— 复用是已有号）
        reused = self._try_claim_reused()
        if reused: return reused

        # 2) 需要拿新号 → 容量门
        if self.max_active > 0:
            deadline = time.time() + self.acquire_timeout
            with self._cap_cond:
                while True:
                    with self._conn() as c:
                        active = self._count_active(c)
                    if active < self.max_active:
                        break
                    # 等待释放（带超时）；醒来后再试一次复用
                    remain = deadline - time.time()
                    if remain <= 0:
                        raise PhonePoolCapacityExhausted(active, self.max_active)
                    self._cap_cond.wait(timeout=min(remain, 5.0))
                    # 醒来先重试复用：可能别人 mark_used 释放了一个可复用号
                    reused = self._try_claim_reused()
                    if reused: return reused

        # 3) 真正下单
        sess = self.provider.acquire(**kw)
        ...
```

**释放/补位触发点**（每处都 `with self._cap_cond: self._cap_cond.notify_all()`）：

- `_mark_used`（号码满 `max_reuse` 后 `status='finished'`，或继续 `reused` 但本 worker 释放）
- `_mark_dead`（`status='dead'`）
- `_release_lease`
- `reconcile`：标 `expired` / `-gone` 的分支
- 心跳检测到 lease lost 的分支

**为什么活跃数从 SQL 算而不是用内存计数器**：`reconcile` 会跨进程改 `status`；多进程跑同一 db 时（虽然当前不这么用）以 SQL 真相为准更稳。计数 cost 极低（小表 + 索引）。

**理由**：

- 复用路径不进容量门 —— 复用本身不增加持号数，应当鼓励
- 唤醒后**先重试复用**再决定是否真下单，最大化复用率
- 超时抛 `PhonePoolCapacityExhausted` 由调用方决定怎么处理（worker 兜底 mark dead 当前任务），避免 worker 永远卡死
- `max_active=0` 表示不限，向后兼容

**替代**：`Semaphore(max_active)` —— 不行：信号量只能记 acquire/release 配对，无法应对 `reconcile` 异步改 status、心跳超时、多进程的场景；condition + SQL 真相更鲁棒。

### D6. 默认 max_active 值

**选择**：默认 `phone_max_active = max_workers`。

**理由**：每个 worker 同时只持有一个号；`max_active = max_workers` 即"每 worker 一个号 + 不溢出"。需要更激进省钱可设 `max_active = max_workers - 1` 强制等待复用；需要更激进吞吐可设更大值。

### D7. TUI 渲染策略 & 布局

**Widget 树**：

```python
class RegisterMonitorApp(App):
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("p", "pause_intake", "Pause new"),
        Binding("r", "resume_intake", "Resume"),
        Binding("f", "cycle_filter", "Filter"),
    ]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield StatusBar(id="status")                       # Static, reactive 聚合
        with Horizontal(id="workers"):
            for i in range(self.max_workers):
                yield WorkerPanel(id=f"w{i+1}")            # Static
        with Horizontal(id="middle"):
            with Vertical(id="features"):
                yield RichLog(id="log-sentinel",   markup=True, max_lines=2000)
                yield RichLog(id="log-email",      markup=True, max_lines=2000)
                yield RichLog(id="log-sms",        markup=True, max_lines=2000)
                yield RichLog(id="log-phone_pool", markup=True, max_lines=2000)
            yield PoolStatsPanel(id="pool-stats")          # Static
        with TabbedContent(id="all-logs"):
            with TabPane("All", id="tab-all"):     yield RichLog(id="log-all",     max_lines=5000)
            with TabPane("✓",   id="tab-success"): yield RichLog(id="log-success", max_lines=2000)
            with TabPane("✗",   id="tab-fail"):    yield RichLog(id="log-fail",    max_lines=2000)
            with TabPane("⚠",   id="tab-warn"):    yield RichLog(id="log-warn",    max_lines=2000)
        yield Footer()
```

**渲染策略**：

- 不维护额外 ring buffer —— `RichLog` 自带 `max_lines` 截断
- `_drain_bus` 在事件循环里跑（`set_interval(0.1, ...)`）：每 100ms 把 bus 队列里所有事件 pop 出来，分别 `query_one("#log-...").write(formatted_line)` + 路由到 worker panel + 路由到 all-logs tab
- `WorkerPanel` 维护本地 `deque(maxlen=8)` 用于压缩显示最近日志；`update()` 重渲整个面板
- `PoolStatsPanel` 由 `set_interval(1.0, refresh_stats)` 调 `pool.stats()` 后 `update()`
- `StatusBar` 用 Textual `reactive` 字段，bus 事件中 level=success/fail 触发计数器递增 → 自动 rerender

**布局示意**（终端 ≥ 120 列）：

```
┌ Header  ChatGPT Register · 14:32 · uptime 0:18 ─ q quit · p pause · f filter ┐
├ Status: ● 3/3 active   ✓ 28   ✗ 4   ⚠ 2   📈 1.6/min   📞 47 ─────────────────┤
├─ W1 ──────────┬─ W2 ──────────┬─ W3 ──────────────────────────────────────┤
│ kim***@..     │ owen***@..    │ idle                                       │
│ step: SMS     │ step: OAuth   │                                            │
│ 0:42          │ 1:15          │                                            │
│ 12:31 send    │ 12:30 captcha │                                            │
│ 12:31 +86 ..  │ 12:30 ok      │                                            │
├───────────────┴───────────────┴────────────────┬───────────────────────────┤
│ ┌ sentinel ────────────────────────────────┐  │ ┌ Pool Stats ────────────┐│
│ │ 12:30:41 sentinel solved (2.1s)          │  │ │ active   : 3 / 3       ││
│ │ ...                            ▲ scroll  │  │ │ fresh    : 12          ││
│ ├ email ───────────────────────────────────┤  │ │ reuse    : 28          ││
│ │ 12:30:14 email created                   │  │ │ rate     : 70.0%       ││
│ │ ...                                      │  │ │ spent    : $1.42       ││
│ ├ sms ─────────────────────────────────────┤  │ │ waiters  : 0           ││
│ │ 12:31:48 ⚠ HeroSMS timeout, switch...    │  │ │ ─ leases ───────────── ││
│ │ ...                                      │  │ │ W1 +1*****1234 1/3 F   ││
│ ├ phone_pool ──────────────────────────────┤  │ │ W2 +1*****5678 2/3 R   ││
│ │ 12:30:02 REUSE +1*****1234 used=1/3      │  │ └────────────────────────┘│
│ └──────────────────────────────────────────┘  │                            │
├──[ All ][ ✓ ][ ✗ ][ ⚠ ]─────────────────────────────────────────────────────┤
│ 12:32:11 [W2][sms]        oauth callback received                          │
│ 12:31:48 [W2][sms]        ⚠ provider switch HeroSMS→Quackr                 │
│ 12:31:02 [W1][phone_pool] ✓ smyers token saved                             │
└─ Footer  Q Quit  P Pause  R Resume  F Filter ─────────────────────────────┘
```

窄终端（< 100 列）：用 Textual CSS media query 把 features 和 pool-stats 改成上下叠放（`Vertical` 替代 `Horizontal`）。

### D8. 非 TTY 降级

**选择**：`main()` 入口先检测 `sys.stdout.isatty()` + env `CHATGPT_REGISTER_NO_TUI` + `--no-tui`：

- TUI 路径：`RegisterMonitorApp(...).run()` —— Textual 接管 stdout
- Fallback 路径：直接跑原有 `ThreadPoolExecutor` 流程，注册 `fallback.TextSubscriber` 把 bus 事件打成 `"YYYY-MM-DD HH:MM:SS [LEVEL][channel][Wn] msg"` 行写 stdout

**理由**：

- Textual 在非 TTY 下会报错或行为退化，必须显式分流
- `nohup` / docker logs / CI / 重定向场景走 fallback，行为与现有 `print` 接近，grep 友好
- 用户在交互终端但想要纯文本（比如 IDE 集成 terminal 不支持完整 ANSI）也能 `--no-tui` 强制降级

### D9. 暂停 / 恢复语义

**选择**：

- "暂停"（`p`）= 不再从待注册列表里取新任务给 worker；正在跑的 worker 继续跑完手头账号
- "恢复"（`r`）= 重新允许取新任务
- 实现：worker 主循环里在每次 `pool.submit` 前检查 `monitor.intake_paused.is_set()`；TUI 的 `action_pause_intake` / `action_resume_intake` 操作这个 `threading.Event`
- 暂停状态在 StatusBar 显示 `⏸ paused`

**理由**：硬中断在跑的 worker 风险高（lease / 邮箱 / OAuth 状态都可能不一致），暂停接新任务是安全的折中。

## Risks / Trade-offs

- **[Textual 接管 stdout 后第三方库 print/warning 会破坏画面]** → Textual 启动时 redirect `sys.stdout`/`sys.stderr` 到一个 capture，每行作为 `system` channel 事件转发到 All Logs；urllib3 / requests warnings 用 `warnings.filterwarnings` + `logging` 桥接到 bus
- **[事件总线队列阻塞 worker]** → `bus.emit()` 用 `queue.put_nowait`，满则 drop + 计数器；StatusBar 显示 dropped 数提醒
- **[`_drain_bus` 单次 pop 太多导致事件循环卡顿]** → 每次 drain 限制最多 N 条（默认 200），剩余下个 tick 处理；丢帧不丢事件
- **[Textual 在某些终端（旧 Windows ConHost / 远程 SSH 损坏 TTY）渲染异常]** → 启动时 try `app.run()`，捕获 Textual 启动异常 → 自动回退 fallback + 警告日志
- **[max_active 等待死锁]** → 用 `acquire_timeout`（默认 60s）+ 抛异常兜底；不允许永久等待
- **[max_active 与 max_reuse 配置冲突]** → 二者正交：`max_active` 是同时持号数，`max_reuse` 是单号复用次数；启动时打印两个值的实际生效值，避免误解
- **[多进程跑同 db 时 condition 唤醒不跨进程]** → 跨进程的等待者要靠 `_cap_cond.wait(timeout=5)` 的兜底自旋；当前用法是单进程，不是问题
- **[fallback 模式输出量增大]** → 可接受（grep / less 处理）；fallback 直接转发不维护额外 buffer
- **[依赖新增 Textual]** → Textual 是纯 Python（依赖 Rich + linkify-it-py 等），无原生扩展，跨平台 OK；首次启动 < 200ms，可接受
- **[Ctrl+C 在 Textual 中默认被吞]** → 显式 `BINDINGS = [Binding("ctrl+c", "quit", show=False), ...]` 与 `q` 一致地走 graceful shutdown

## Migration Plan

1. 先合并 `monitor/bus.py` + `monitor/fallback.py`；用 `bus` 替换 `_print_lock + print`；此时 TUI 还没接，所有运行都走 fallback，行为等价于现状
2. 接 `phone_pool` 的 `stats()` API 与 `max_active` 容量门（默认 `max_active=0` 不影响行为）
3. 接 `monitor/app.py` Textual 应用 + 桥接；CLI 加 `--no-tui`；config 加 `phone_max_active`、`tui_enabled`
4. 灰度：先在本地短任务（`total_accounts=2~3`）跑通，观察 fallback / TUI 切换、键盘绑定、暂停语义；再上长任务
5. 回滚：保留 `--no-tui` 即可拿回 fallback 行为（与现状等价）；`phone_max_active=0` 即可关掉容量门

## Open Questions

- TUI 默认开启 vs 默认关闭？（提案中默认开启；若 CI 友好性更重要可改默认关闭，由 `--tui` 显式开）
- `phone_max_active` 默认值是 `max_workers` 还是 `max_workers + 1`？（提案中 = `max_workers`，等用户跑一段时间反馈再调）
- 是否要把事件 mirror 到日志文件（`./logs/run-YYYYMMDD.log`）？建议加，但默认关闭，避免磁盘占用
