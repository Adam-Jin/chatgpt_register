from __future__ import annotations

import time
from collections import deque
from typing import Iterable, Optional

from rich.table import Table
from textual.reactive import reactive
from textual.widgets import Static


class StatusBar(Static):
    active_workers = reactive(0)
    max_workers = reactive(0)
    done = reactive(0)
    success = reactive(0)
    fail = reactive(0)
    warn = reactive(0)
    rate = reactive(0.0)
    paused = reactive(False)
    dropped = reactive(0)
    uptime_seconds = reactive(0.0)
    run_state = reactive("running")
    status_hint = reactive("")

    def render(self) -> str:
        paused = " | paused" if self.paused else ""
        hint = f" | {self.status_hint}" if self.status_hint else ""
        return (
            f"state {self.run_state} | "
            f"workers {self.active_workers}/{self.max_workers} | "
            f"done {self.done} | ok {self.success} | fail {self.fail} | "
            f"warn_evt {self.warn} | rate {self.rate:.2f}/min | "
            f"uptime {int(self.uptime_seconds)}s | dropped {self.dropped}{paused}{hint}"
        )


class WorkerPanel(Static):
    def __init__(self, worker_id: str, **kwargs):
        super().__init__(**kwargs)
        self.worker_id = worker_id
        self.account: str = "-"
        self.step: str = "-"
        self.started_at: Optional[float] = None
        self.finished_at: Optional[float] = None
        self.state: str = "idle"
        self.recent_logs: deque[str] = deque(maxlen=8)

    def set_account(self, account: Optional[str]) -> None:
        if self.state != "active":
            self.started_at = time.time()
            self.finished_at = None
        self.account = account or "-"
        self.state = "active"
        self.refresh()

    def set_step(self, step: Optional[str]) -> None:
        if self.state != "active":
            self.started_at = time.time()
            self.finished_at = None
        self.step = step or "-"
        self.state = "active"
        self.refresh()

    def add_log(self, line: str) -> None:
        self.recent_logs.append(line)
        self.refresh()

    def mark_idle(self) -> None:
        if self.state == "active":
            self.finished_at = time.time()
        self.state = "idle"
        if self.step == "-":
            self.step = "done"
        self.refresh()

    def clear(self) -> None:
        self.account = "-"
        self.step = "-"
        self.started_at = None
        self.finished_at = None
        self.state = "idle"
        self.recent_logs.clear()
        self.refresh()

    def render(self) -> str:
        elapsed = 0
        if self.started_at is not None:
            end_ts = self.finished_at if self.finished_at is not None else time.time()
            elapsed = int(end_ts - self.started_at)
        body = "\n".join(self.recent_logs) if self.recent_logs else "-"
        return (
            f"{self.worker_id}\n"
            f"state: {self.state}\n"
            f"acct: {self.account}\n"
            f"step: {self.step}\n"
            f"elapsed: {elapsed}s\n"
            f"{body}"
        )


class PoolStatsPanel(Static):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.snapshot: dict = {}

    def update_snapshot(self, snapshot: Optional[dict]) -> None:
        self.snapshot = snapshot or {}
        self.refresh()

    def render(self):
        table = Table.grid(padding=(0, 1))
        stats = self.snapshot or {}
        table.add_row("active", f"{stats.get('active', 0)}/{stats.get('max_active', 0)}")
        table.add_row("fresh", str(stats.get("fresh_total", 0)))
        table.add_row("reuse", str(stats.get("reuse_total", 0)))
        table.add_row("rate", f"{float(stats.get('reuse_rate', 0.0)) * 100:.1f}%")
        table.add_row("spent", f"${float(stats.get('spent', 0.0)):.4f}")
        table.add_row("waiters", str(stats.get("cap_waiters", 0)))
        leases = list(_format_leases(stats.get("leases") or []))
        if leases:
            table.add_row("leases", "\n".join(leases))
        return table


def _format_leases(leases: Iterable[dict]) -> Iterable[str]:
    for lease in leases:
        worker = lease.get("worker_id") or "?"
        phone = lease.get("phone_number") or "?"
        used = lease.get("used_count", 0)
        max_reuse = lease.get("max_reuse", 0)
        origin = "R" if lease.get("is_reused") else "F"
        yield f"{worker} {phone} {used}/{max_reuse} {origin}"
