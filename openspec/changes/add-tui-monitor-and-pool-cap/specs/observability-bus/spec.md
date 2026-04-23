## ADDED Requirements

### Requirement: Structured Event Bus

The system SHALL provide a process-singleton `EventBus` that all logging output (worker main loop, sentinel solver, email pools, SMS providers, phone pool) routes through, replacing direct `print()` calls. Each emitted event SHALL carry `timestamp`, `channel`, `worker_id` (optional), `level`, `message`, and arbitrary structured `fields`.

#### Scenario: Worker emits event with auto-attached worker_id
- **WHEN** code running inside a worker thread (registered via `monitor.set_current_worker("W1")`) calls `bus.emit("phone_pool", "REUSE +1******")`
- **THEN** the event delivered to subscribers SHALL have `worker_id="W1"`, `channel="phone_pool"`, `level="info"`, and a monotonically non-decreasing `timestamp`

#### Scenario: System-level event with no worker context
- **WHEN** code outside any worker (e.g. startup, reconcile) calls `bus.emit("system", "phone_pool reconcile complete")`
- **THEN** the event SHALL have `worker_id=None` and be routed to the system / global channel

#### Scenario: Non-blocking emit under back-pressure
- **WHEN** the subscriber queue is full and `bus.emit(...)` is called
- **THEN** the event SHALL be dropped without blocking the caller, and a `dropped_events` counter SHALL increment, observable via `bus.stats()`

### Requirement: Channel Adapter for Submodules

The system SHALL provide `bus.channel(name)` that returns a `Callable[[str], None]`. Submodules (`PhonePool`, `SentinelSolver`, mail / SMS pools) which already accept a `log` constructor parameter SHALL be wired with this adapter so their internal log calls flow into the bus on the named channel — without modifying the submodules themselves.

#### Scenario: PhonePool wired to phone_pool channel
- **WHEN** `PhonePool` is constructed with `log=bus.channel("phone_pool")` and internally calls `self.log("[phone_pool] REUSE ...")`
- **THEN** the bus SHALL receive an event with `channel="phone_pool"` carrying the message string

### Requirement: Multiple Independent Subscribers

The bus SHALL support multiple concurrent subscribers (e.g. TUI renderer + file logger + fallback text printer) where each subscriber receives every event independently and the failure / slowness of one subscriber SHALL NOT block others.

#### Scenario: One subscriber stalls, others keep receiving
- **WHEN** subscriber A stops draining its queue while subscriber B drains normally
- **THEN** subscriber B SHALL continue to receive events; subscriber A's queue MAY drop events but SHALL NOT block emission

### Requirement: Fallback Text Output

When TTY is unavailable (`sys.stdout.isatty()` is `False`) or the env var `CHATGPT_REGISTER_NO_TUI=1` is set or the CLI flag `--no-tui` is passed, the system SHALL register a text subscriber that prints each event as `"YYYY-MM-DD HH:MM:SS [LEVEL][channel][Wn] message"` to stdout, preserving grep-ability.

#### Scenario: Stdout is redirected to a file
- **WHEN** the program is launched as `python chatgpt_register.py > run.log` (non-TTY stdout)
- **THEN** no TUI SHALL render and every event SHALL appear as one line in `run.log` with timestamp, level, channel and worker prefix

#### Scenario: Explicit --no-tui flag overrides TTY detection
- **WHEN** the program is launched in an interactive terminal but with `--no-tui`
- **THEN** the system SHALL use the text subscriber and SHALL NOT take over the terminal

### Requirement: Backward Compatibility

The bus SHALL not require modification of submodules' public APIs. Submodules SHALL remain runnable / testable / importable as standalone libraries with a plain `log=print` callback.

#### Scenario: Submodule used standalone with print
- **WHEN** `PhonePool(provider, log=print)` is constructed in a unit test (no bus initialized)
- **THEN** it SHALL function exactly as before this change, writing to stdout via `print`
