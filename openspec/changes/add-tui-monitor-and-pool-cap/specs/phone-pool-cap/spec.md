## ADDED Requirements

### Requirement: Concurrent Active-Lease Cap

`PhonePool` SHALL accept a `max_active` configuration value (default `0` meaning unlimited; recommended default in `chatgpt_register.py` `= max_workers`). When `max_active > 0`, the count of phone-pool rows whose `status IN ('fresh', 'reused')` AND (`end_at IS NULL` OR `end_at > now`) SHALL NOT exceed `max_active` at any time as observed by `acquire_or_reuse()`.

#### Scenario: Cap respected when fallback to provider.acquire would exceed it
- **WHEN** `max_active=3`, current active count is `3`, and a worker calls `acquire_or_reuse()` and reuse is not possible
- **THEN** `acquire_or_reuse()` SHALL NOT call `provider.acquire()`; it SHALL block until the active count drops below `max_active` or a configured timeout elapses

#### Scenario: Reuse path bypasses the cap
- **WHEN** `max_active=3`, current active count is `3`, and a reusable number is available (`status='reused'`, lease free, `used_count < max_reuse`, end_at safe)
- **THEN** `acquire_or_reuse()` SHALL succeed via `_try_claim_reused()` without waiting, because reusing an existing number does not increase the active count

#### Scenario: Backward-compatible unlimited mode
- **WHEN** `max_active=0` (default)
- **THEN** the cap SHALL be disabled and behavior SHALL match the pre-change `acquire_or_reuse()` exactly

### Requirement: Dynamic Refill on Release

When a phone-pool slot becomes free — via `_mark_used` (number finished or returned to reused pool), `_mark_dead`, `_release_lease`, `reconcile` marking a number `expired`, or heartbeat detecting a lost lease — `PhonePool` SHALL notify any threads waiting on the cap so they re-evaluate and may proceed.

#### Scenario: mark_dead unblocks a waiter
- **WHEN** worker `W2` is blocked in `acquire_or_reuse()` because `max_active` is reached, and worker `W1` then calls `lease.mark_dead("openai_rejected")` which transitions the row to `status='dead'`
- **THEN** `W2` SHALL wake up within at most one wait-tick (≤ 5 s), observe the new active count `< max_active`, attempt reuse first, then proceed to `provider.acquire()` if reuse still misses

#### Scenario: max_reuse exhaustion frees the slot
- **WHEN** worker `W1` calls `lease.mark_used(...)` causing `used_count` to reach `max_reuse`, transitioning `status` to `finished`
- **THEN** waiting workers SHALL be notified and the slot SHALL be available for a new acquire

#### Scenario: Reconcile expiry frees the slot
- **WHEN** `reconcile()` marks a number `expired` because the cloud-side `estDate` is in the past
- **THEN** any threads waiting on the cap SHALL be notified

### Requirement: Acquire Timeout

`acquire_or_reuse()` SHALL accept (via `PhonePool.__init__`) an `acquire_timeout` (default `60.0` seconds). If a thread is blocked waiting for the cap and the timeout elapses without the active count dropping below `max_active`, it SHALL raise `PhonePoolCapacityExhausted` (a new exception type exported from `phone_pool`).

#### Scenario: Timeout exceeded
- **WHEN** `max_active=3`, active count stays at `3` for the entire `acquire_timeout` window, and no reuse becomes possible
- **THEN** the blocked `acquire_or_reuse()` call SHALL raise `PhonePoolCapacityExhausted` with attributes `current_active` and `max_active`

#### Scenario: Wake-up retries reuse before re-blocking
- **WHEN** a waiter is woken by a notify and the cap is satisfied
- **THEN** it SHALL first call `_try_claim_reused()` again (a number may have just become reusable) before falling back to `provider.acquire()`

### Requirement: Stats Snapshot API

`PhonePool` SHALL expose a `stats()` method returning a dict with at least the keys: `active` (int), `max_active` (int), `fresh_total` (int — count of rows ever inserted as `status='fresh'` this run), `reuse_total` (int — sum of all `_try_claim_reused` hits this run), `reuse_rate` (float, 0.0–1.0), `spent` (float, USD, sum of `cost` across all numbers), `leases` (list of `{worker_id, activation_id, phone_number, used_count, max_reuse, is_reused}`), and `cap_waiters` (int — current number of threads blocked on the cap). The method SHALL be safe to call from any thread and SHALL NOT block emit / acquire paths.

#### Scenario: TUI polls stats every second
- **WHEN** the TUI calls `pool.stats()` once per second while workers are running
- **THEN** the call SHALL return within milliseconds and SHALL reflect the current SQLite-truth state of the pool

#### Scenario: cap_waiters reported during contention
- **WHEN** `max_active=3`, active count is `3`, and 2 workers are blocked waiting for the cap
- **THEN** `stats()['cap_waiters']` SHALL equal `2`

### Requirement: Configuration Surface

The `phone_max_active` value SHALL be configurable via (in priority order): CLI flag / env var `PHONE_MAX_ACTIVE`, `config.json` `phone_max_active` field, default `= max_workers`. The `acquire_timeout` SHALL be configurable via `phone_acquire_timeout` in `config.json`.

#### Scenario: Env override takes precedence
- **WHEN** `config.json` has `phone_max_active: 2` and env `PHONE_MAX_ACTIVE=5` is set
- **THEN** the effective `max_active` SHALL be `5`

#### Scenario: Default tracks max_workers
- **WHEN** no override is present and `max_workers=3`
- **THEN** the effective `max_active` SHALL be `3`
