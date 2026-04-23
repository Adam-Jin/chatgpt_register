## Why

并发注册（默认 `max_workers=3`，`ThreadPoolExecutor`）当前所有日志通过 `_print_lock` 串行打到同一个 stdout，worker、`sentinel_solver`、邮箱池、`phone_pool` 的输出交织在一起，运行中很难判断每个 worker 的实时状态、各子系统的健康度，以及电话号码池的复用/申请情况。

更危险的是：`phone_pool` 只有"单号最多复用 N 次"（`max_reuse`）的限制，缺乏"同时持有的活跃号码总数"上限。当复用逻辑因为 bug、provider 状态或 lease 抢占失败而走不到 `_try_claim_reused` 命中路径时，会持续走 `provider.acquire()` 的 fallback，悄悄烧钱开新号。需要一个并发持号上限，并在已有号码 `mark_dead`/`expired`/复用满之后能动态补位。

## What Changes

- 新增 **Textual TUI 应用**，把当前 `print()` 输出替换为结构化事件总线 → 多 widget 渲染，并提供键盘交互：
  - 顶部 Header：全局聚合（worker 在跑数 / 完成 / 成功 / 失败 / 速率 / uptime）
  - 中间 Worker 区：每个 worker 一个 `Static` 面板（账号、当前步骤、计时、最近 N 行日志）；点击可切到详情视图
  - Feature 区：每个子系统一个独立可滚动 `RichLog` —— `sentinel`、`email`、`sms`、`phone_pool`；支持鼠标滚轮 / PgUp/PgDn 翻历史
  - Pool Stats 面板：电话号码池实时统计（活跃数 / 上限 / 申请累计 / 复用累计 / 复用率 / 花费 / 等待容量门的线程数）
  - 底部 All Logs：`TabbedContent` 切「全部 / ✓ / ✗ / ⚠」过滤
  - 键盘绑定：`q` 退出、`p` 暂停接新任务（让在跑 worker 跑完当前账号）、`r` 恢复、`f` 在 All Logs 上切 filter、`1..9` 切到对应 worker 面板
- 新增 **结构化事件总线** `monitor.bus`：所有 `print()` 调用迁移到 `bus.emit(channel, worker_id, level, msg, **fields)`；非 TUI 模式（`--no-tui` 或非 TTY）退化为带 `[channel][Wn]` 前缀的纯文本输出，行为向后兼容
- `phone_pool` 暴露**统计快照** API：`PhonePool.stats()` 返回 `{active, max_active, fresh_total, reuse_total, reuse_rate, spent, leases: [...]}`，TUI 每秒刷新
- **新增 `phone_pool` 并发持号上限**：
  - 配置项 `phone_max_active`（默认 = `max_workers`，可在 `config.json` / 环境变量覆盖）
  - `acquire_or_reuse()` 在调 `provider.acquire()` 前检查"当前 status ∈ {fresh, reused} 且未过期"的号码总数；达到上限时**阻塞等待**（带超时）而非直接开新号
  - 当任意号被 `mark_dead` / `mark_used` 触发 `status='finished'/'dead'` / `reconcile` 标 `expired` / 复用次数达 `max_reuse` 后自动 `finished` —— 释放槽位，唤醒等待者（动态补位）
  - 等待超时（默认 60s）抛 `PhonePoolCapacityExhausted`，由 worker 兜底 `mark_dead` 当前任务，避免死锁
- TUI 默认开启，可通过 `--no-tui` CLI flag 或 `CHATGPT_REGISTER_NO_TUI=1` 关闭

不破坏现有 CLI 入口和 `data.db` schema —— `phone_pool` 表新增字段（如有）通过 `CREATE INDEX IF NOT EXISTS` 风格的轻量迁移完成。

## Capabilities

### New Capabilities
- `tui-monitor`: 基于 Rich 的实时 TUI 面板，订阅事件总线，按 worker / 子系统分面板渲染日志和聚合统计；非 TTY 环境自动降级为文本输出
- `observability-bus`: 结构化事件总线（channel + worker_id + level + fields），统一 worker 主流程、`sentinel_solver`、邮箱池、`sms_provider`、`phone_pool` 的日志出口
- `phone-pool-cap`: `phone_pool` 的并发持号上限 + 动态补位机制（含等待 / 超时 / 唤醒 / 统计 API）

### Modified Capabilities
（无 —— 项目当前 `openspec/specs/` 为空，所有相关行为首次在规范层定义。）

## Impact

- **代码**：
  - `chatgpt_register.py`：替换 `_print_lock + print()` 为 `bus.emit(...)`；为 worker 主循环注入 `worker_id`；`main` 启动 TUI runtime；新增 `--no-tui` flag
  - `phone_pool.py`：新增 `max_active` 字段、`_count_active_locked()`、`acquire_or_reuse` 容量门 + condition 等待、`stats()` 快照；释放路径（`_mark_used`/`_mark_dead`/`reconcile` 标 expired）触发 `notify`
  - `sentinel_solver.py` / `qq_mail_pool.py` / `herosms_pool.py` / `quackr_pool.py` / `sms_provider.py`：注入 `log` 回调（已有 `log` 参数的模块改为默认 `bus.channel("sentinel"/"email"/"sms")`）
  - 新增 `monitor/` 目录：`bus.py`（事件总线）、`app.py`（Textual `App` + widgets）、`fallback.py`（文本前缀输出）、`bridge.py`（worker 线程 ↔ Textual 事件循环桥接）
- **依赖**：新增 `textual>=0.80`（已 transitively 带 Rich；`requirements_solver.txt` 增加一行）
- **配置**：`config.json` 新增 `phone_max_active`、`tui_enabled` 字段；环境变量新增 `PHONE_MAX_ACTIVE`、`CHATGPT_REGISTER_NO_TUI`
- **数据库**：`phone_pool` 表无 schema 变更（活跃数从现有 `status` + `end_at` 字段计算）
- **运行时行为**：开启 TUI 后 stdout 由 Rich 接管；CI / nohup / 重定向到文件场景需依赖 fallback 模式
