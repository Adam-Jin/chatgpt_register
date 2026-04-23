## 1. Foundation: Event Bus & Fallback

- [x] 1.1 Add `textual>=0.80` to `requirements_solver.txt` (Rich comes transitively); create `requirements.txt` for the main app if missing
- [x] 1.2 Create `monitor/` package: `monitor/__init__.py`, `monitor/bus.py`, `monitor/fallback.py`, `monitor/app.py`, `monitor/widgets.py`
- [x] 1.3 Implement `monitor/bus.py`: `Event` dataclass, `EventBus` singleton with `emit / subscribe / channel / stats`, thread-local `current_worker_id`, non-blocking `put_nowait` + drop counter
- [x] 1.4 Implement `monitor/fallback.py`: `TextSubscriber` that drains the bus queue and prints `"YYYY-MM-DD HH:MM:SS [LEVEL][channel][Wn] msg"` lines
- [x] 1.5 Implement `monitor/__init__.py`: `run_with_monitor(run_callable, *, tui_enabled, max_workers)` — if TUI: instantiates `RegisterMonitorApp(run_callable, max_workers)` and calls `app.run()`; else: registers `fallback.TextSubscriber` and calls `run_callable()` directly. Auto-falls back on Textual init exception.
- [x] 1.6 Unit tests: bus emit ordering, drop counter under back-pressure, channel routing, fallback line format

## 2. Wire Submodules to Bus (no behavior change)

- [ ] 2.1 In `chatgpt_register.py`, replace `_print_lock + print(...)` call sites with `bus.emit("worker", msg, ...)`; keep `_print_lock` only if any callers remain after migration, otherwise remove
- [x] 2.2 At `PhonePool` construction site (`chatgpt_register.py:202`), pass `log=bus.channel("phone_pool")`
- [x] 2.3 At `SentinelSolver` construction site, pass `log=bus.channel("sentinel")`
- [x] 2.4 At DuckMail / `qq_mail_pool` construction sites, pass `log=bus.channel("email")`
- [ ] 2.5 At HeroSMS / Quackr / `sms_provider` construction sites, pass `log=bus.channel("sms")`
- [x] 2.6 In `submit_account` worker dispatch, set `monitor.set_current_worker(f"W{idx+1}")` at the top of the worker function and clear on exit
- [ ] 2.7 Smoke test: run with `--no-tui`, verify all expected log lines appear with correct `[channel][Wn]` prefixes and ordering

## 3. Phone Pool: stats() snapshot API

- [x] 3.1 Add `PhonePool.stats()` returning `{active, max_active, fresh_total, reuse_total, reuse_rate, spent, leases, cap_waiters}`; compute `active` and `spent` via SQL, `fresh_total` / `reuse_total` via in-memory counters incremented in `acquire_or_reuse` / `_try_claim_reused`
- [x] 3.2 Add `leases` listing: cross-reference current `lease_owner == self.owner_id` rows with an in-memory `{activation_id: worker_id}` map populated when `PhoneLease` is created
- [x] 3.3 Unit tests: stats with 0 / partial / fully-leased pool; reuse_rate math; thread safety (concurrent stats() while acquire runs)

## 4. Phone Pool: Concurrent Active-Lease Cap

- [x] 4.1 Extend `PhonePool.__init__` with `max_active: int = 0`, `acquire_timeout: float = 60.0`; create `self._cap_cond = threading.Condition()`; expose `cap_waiters` counter
- [x] 4.2 Add `_count_active_locked(c) -> int`: SQL `SELECT COUNT(*) FROM phone_pool WHERE status IN ('fresh','reused') AND (end_at IS NULL OR end_at > ?)`
- [x] 4.3 Add new exception class `PhonePoolCapacityExhausted(Exception)` exported from `phone_pool.py`
- [x] 4.4 Modify `acquire_or_reuse`: after the initial `_try_claim_reused` miss, enter `with self._cap_cond:` loop — check active count, wait with timeout if at cap, retry `_try_claim_reused` after each wake, raise `PhonePoolCapacityExhausted` on overall timeout
- [x] 4.5 Add `_notify_cap()` helper; call it from `_mark_used`, `_mark_dead`, `_release_lease`, `reconcile` (every branch that may decrement active count), and the heartbeat lease-lost path
- [x] 4.6 Unit tests: cap enforced under fallback, reuse bypasses cap, mark_dead unblocks waiter within ≤5 s, reconcile expiry unblocks waiter, timeout raises `PhonePoolCapacityExhausted`, `max_active=0` matches old behavior exactly

## 5. Phone Pool: Configuration Surface

- [x] 5.1 Add `phone_max_active` and `phone_acquire_timeout` to default config in `chatgpt_register.py` `_CONFIG`
- [x] 5.2 Wire env override: `PHONE_MAX_ACTIVE`, `PHONE_ACQUIRE_TIMEOUT` (with fallback to config / `max_workers`)
- [x] 5.3 Pass resolved values into `PhonePool(...)` at construction
- [x] 5.4 Print effective `max_workers / max_active / max_reuse / acquire_timeout` in startup banner so config issues are obvious

## 6. Worker: Handle PhonePoolCapacityExhausted

- [x] 6.1 In the worker's phone-acquire path, catch `PhonePoolCapacityExhausted`, emit a `warn`-level event, and fail this account with a clear reason (skip without infinite retry); do NOT crash the worker
- [x] 6.2 Increment a "skipped due to pool cap" counter exposed in the run summary

## 7. TUI: Textual App, Widgets, Bridge

- [ ] 7.1 Implement `monitor/widgets.py`: `StatusBar(Static)` with `reactive` fields (`active_workers / done / success / fail / rate / uptime / paused / dropped`), `WorkerPanel(Static)` (account, step, elapsed, recent-log deque maxlen=8), `PoolStatsPanel(Static)` (fields from `pool.stats()`)
- [ ] 7.2 Implement `monitor/app.py` `RegisterMonitorApp(App)`: `compose()` lays out `Header` → `StatusBar` → `Horizontal` of N `WorkerPanel` → `Horizontal` of `Vertical(features RichLogs)` + `PoolStatsPanel` → `TabbedContent(All/✓/✗/⚠ RichLogs)` → `Footer`. Use Textual CSS for sizing and the narrow-terminal collapse rule.
- [ ] 7.3 Bridge: `on_mount` schedules `self._drain_bus` via `set_interval(0.1, ...)` and `self._refresh_pool_stats` via `set_interval(1.0, ...)`; starts the worker thread via `self.run_worker(self._kickoff_workers, thread=True)` which inside runs the original `ThreadPoolExecutor` flow
- [ ] 7.4 `_drain_bus`: pop up to N=200 events per tick, format `[HH:MM:SS][Wn] msg`, route to `query_one(f"#log-{channel}", RichLog).write(...)`, route to corresponding `WorkerPanel` (if `worker_id` set), route to `#log-all` always, route to level-tab if level matches
- [ ] 7.5 Aggregate counters: `_drain_bus` increments `StatusBar.success / fail / warn` reactives based on event level + structured fields; uptime / rate computed from `set_interval(1.0)`
- [ ] 7.6 `_refresh_pool_stats`: call `pool.stats()`, update `PoolStatsPanel` fields including masked phone numbers (`+1*****1234`) and lease attribution
- [ ] 7.7 `BINDINGS`: `q`/`ctrl+c` quit, `p` pause intake, `r` resume intake, `f` cycle filter tabs, `1-9` focus worker panel; expose `intake_paused: threading.Event` shared with worker dispatcher
- [ ] 7.8 Capture `sys.stdout` / `sys.stderr` (third-party noise) and `warnings` → emit on `system` channel so they show up in All Logs instead of breaking the UI
- [ ] 7.9 Auto-fallback: if `App.run()` raises `TextualError` / TTY error during startup, log warning, register `TextSubscriber`, run the executor directly
- [ ] 7.10 Smoke test on a 2-account run: TUI renders, channel logs scrollable, `p` / `r` toggle works, `q` exits cleanly with summary printed after terminal restore

## 8. CLI / Config Wiring

- [x] 8.1 Add `--no-tui` argparse flag in `chatgpt_register.py` main entry
- [x] 8.2 Honor `CHATGPT_REGISTER_NO_TUI=1` env var
- [ ] 8.3 Add `tui_enabled` field to `config.json` default (`true`)
- [x] 8.4 Wrap the main `ThreadPoolExecutor` flow in a `run_callable()`; replace the top-level invocation with `monitor.run_with_monitor(run_callable, tui_enabled=..., max_workers=...)` so TUI lifecycle is tied to the run

## 9. Graceful Shutdown

- [ ] 9.1 In `RegisterMonitorApp`, override `action_quit` to set the worker dispatcher's `shutdown` event, wait up to `quit_grace_seconds` (default 120) for in-flight workers, then `app.exit()`
- [ ] 9.2 Bind `ctrl+c` to the same quit action; ensure no `KeyboardInterrupt` reaches Python's default handler while TUI is up
- [ ] 9.3 After Textual exits (or fallback completes), print one-line summary to stdout: success / fail / spent / dropped-events / cap-skipped
- [ ] 9.4 Ensure exit code preserved (non-zero on failures)

## 10. Validation & Docs

- [x] 10.1 Run `openspec validate add-tui-monitor-and-pool-cap --strict` and fix any issues
- [ ] 10.2 Manual run: `total_accounts=3 max_workers=3` with TUI enabled — verify worker panels, pool stats, cap enforcement (set `phone_max_active=1` to force contention)
- [ ] 10.3 Manual run: `--no-tui` redirected to a file — verify text logs are complete and grep-able
- [ ] 10.4 Update `README.md` / `NOTES.md`: TUI screenshot or ASCII layout, new CLI flag, new config fields, `PhonePoolCapacityExhausted` semantics
