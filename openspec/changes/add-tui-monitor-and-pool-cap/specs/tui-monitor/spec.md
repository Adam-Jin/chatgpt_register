## ADDED Requirements

### Requirement: Textual TUI Application with Header, Workers, Features, Pool Stats, and Global Log Panels

When TUI mode is active, the system SHALL run a Textual `App` containing distinct widgets for: (a) global status bar aggregates, (b) per-worker panels (one per concurrent worker), (c) per-feature scrollable `RichLog` widgets for channels `sentinel`, `email`, `sms`, `phone_pool`, (d) a phone-pool statistics panel, and (e) a tabbed global log feed with `All / ✓ / ✗ / ⚠` tabs. UI updates SHALL execute on the Textual event loop and SHALL NOT block worker threads.

#### Scenario: Status bar shows aggregate counters
- **WHEN** TUI is running and 3 workers are active with 12 done / 10 success / 2 fail
- **THEN** the status bar widget SHALL display the active count `3/3`, completed `12`, success `10`, failure `2`, throughput in `accounts/min`, and uptime, updated reactively as events arrive

#### Scenario: Per-worker panel reflects current step and recent log lines
- **WHEN** worker `W1` is on step `OAuth callback` for account `kim***` for `0:42`
- **THEN** the `W1` panel SHALL show the masked account, the step name, elapsed time, and the most recent N log lines emitted with `worker_id="W1"`

#### Scenario: Feature `RichLog` widgets segregate logs by channel and are independently scrollable
- **WHEN** events are emitted on channels `sentinel`, `email`, `sms`, `phone_pool` from any worker
- **THEN** each event SHALL appear in the `RichLog` widget for its channel (and only that channel), with the `[Wn]` worker prefix inline; the user SHALL be able to scroll back through that channel's history independently of other channels using mouse wheel or PgUp/PgDn

#### Scenario: All-Logs tabbed view merges and filters
- **WHEN** any event is emitted on any channel
- **THEN** it SHALL appear in the `All` tab in chronological order prefixed with `[channel][Wn]`, AND it SHALL appear in the `✓ / ✗ / ⚠` tab whose filter matches its level (`success / error / warn`)

### Requirement: Phone Pool Stats Panel

The TUI SHALL render a panel showing real-time phone-pool statistics sourced from `PhonePool.stats()`: `active` count, configured `max_active` cap, total `fresh` numbers acquired this run, total `reuse` count, `reuse_rate` percentage, total `spent` (USD), and a list of currently held leases with `worker_id`, masked phone number, `used_count/max_reuse`, and origin (fresh / reused).

#### Scenario: Reuse rate updates as numbers are reused
- **WHEN** `PhonePool` has acquired 10 fresh numbers and reused them 20 times across worker runs
- **THEN** the stats panel SHALL show `fresh 10  reuse 20  rate 66.7%`

#### Scenario: Active leases listed with worker attribution
- **WHEN** workers `W1` and `W2` each currently hold a lease and `W3` is idle
- **THEN** the leases sub-list SHALL show 2 rows with `W1` / `W2` annotations and SHALL NOT show a row for `W3`

### Requirement: TUI Toggle and Default

The TUI SHALL be enabled by default when stdout is a TTY. The system SHALL accept the CLI flag `--no-tui` and the env var `CHATGPT_REGISTER_NO_TUI=1` to force-disable the TUI.

#### Scenario: Default behavior in interactive terminal
- **WHEN** the program starts in an interactive shell with no overrides
- **THEN** the TUI SHALL initialize and take over rendering

#### Scenario: TUI disabled by env var
- **WHEN** `CHATGPT_REGISTER_NO_TUI=1` is set
- **THEN** the TUI SHALL NOT initialize regardless of TTY status

### Requirement: Keyboard Bindings

The TUI SHALL register the following keyboard bindings, visible in the Footer widget:

- `q` or `ctrl+c`: graceful quit
- `p`: pause new-task intake (in-flight workers continue)
- `r`: resume new-task intake
- `f`: cycle the All-Logs filter tabs forward (`All → ✓ → ✗ → ⚠ → All`)
- `1`..`9`: focus the corresponding worker panel `W1`..`W9`

#### Scenario: Pressing `p` pauses intake
- **WHEN** workers `W1` and `W2` are mid-task and the user presses `p`
- **THEN** the system SHALL stop dispatching new accounts to free workers (`W3` SHALL stay idle even if a phone-pool slot frees), the status bar SHALL show `⏸ paused`, and `W1` / `W2` SHALL continue their current accounts to completion

#### Scenario: Pressing `r` resumes intake
- **WHEN** the system is paused and the user presses `r`
- **THEN** the system SHALL resume dispatching new accounts and the `⏸ paused` indicator SHALL disappear

### Requirement: Sync ↔ Async Bridge Safety

Worker threads (running synchronous business code via `ThreadPoolExecutor`) SHALL emit events via `bus.emit(...)` which is thread-safe and non-blocking. The Textual `App` SHALL drain the bus from inside its own event loop (e.g. `set_interval`) and SHALL be the only entity that mutates widget state. Worker threads SHALL NOT call Textual widget methods directly.

#### Scenario: High-frequency emit from many workers does not block UI
- **WHEN** all workers emit > 100 events/sec collectively
- **THEN** the UI SHALL continue to render at its configured cadence (≥ 4 fps), worker `bus.emit` calls SHALL return in O(microseconds), and any back-pressure SHALL surface as a `dropped_events` counter rather than as worker stalls or UI freezes

### Requirement: Graceful Shutdown

When the program receives `q`, `ctrl+c`, or `KeyboardInterrupt`, or completes normally, the system SHALL stop the Textual `App` cleanly, restore the terminal, drain remaining bus events to stdout (or a log file), and print a final summary line to stdout.

#### Scenario: Quit during a run
- **WHEN** the user presses `q` while workers are still running
- **THEN** the TUI SHALL signal a quit, in-flight workers SHALL be allowed to finish their current account (with a configurable max wait, default 120 s), the terminal SHALL be restored, and the final summary (success / failure counts, total spent, dropped events) SHALL appear as plain text

#### Scenario: Auto-fallback on Textual init failure
- **WHEN** the program is launched with TUI enabled but `App.run()` raises during startup (broken TTY, terminal too small, unsupported terminal)
- **THEN** the system SHALL log a warning, register the fallback text subscriber, and continue executing the run as if `--no-tui` were passed
