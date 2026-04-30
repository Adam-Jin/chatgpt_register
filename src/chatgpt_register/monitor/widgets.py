from __future__ import annotations

import time
from typing import Iterable, Optional

from rich.table import Table
from rich.text import Text
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

    pool_fresh = reactive(0)
    pool_reuse = reactive(0)
    pool_active = reactive(0)
    pool_max_active = reactive(0)
    pool_waiters = reactive(0)
    pool_spent = reactive(0.0)

    viewing_worker = reactive("")
    viewing_state = reactive("")
    viewing_step = reactive("")
    viewing_elapsed = reactive(0)

    def render(self) -> Text:
        line1 = Text()
        line1.append(
            f"{self.run_state} | W {self.active_workers}/{self.max_workers} | "
            f"done {self.done} | ok {self.success} | fail {self.fail} | "
            f"rate {self.rate:.2f}/min",
            style="white",
        )
        if self.paused:
            line1.append(" | paused", style="bold yellow")
        if self.dropped:
            line1.append(f" | dropped {self.dropped}", style="bold yellow")
        if self.status_hint:
            line1.append(f" | {self.status_hint}", style="dim")

        line2 = Text()
        if self.viewing_worker:
            line2.append(
                f"viewing {self.viewing_worker} | {self.viewing_state or '-'} | "
                f"{self.viewing_step or '-'} | {int(self.viewing_elapsed)}s",
                style="cyan",
            )
        else:
            total_uses = int(self.pool_fresh) + int(self.pool_reuse)
            line2.append(
                f"pool: fresh {self.pool_fresh} | reuse {self.pool_reuse} | "
                f"uses {total_uses} | active {self.pool_active}/{self.pool_max_active} | "
                f"wait {self.pool_waiters} | ${self.pool_spent:.2f}",
                style="cyan",
            )
        line1.append("\n")
        line1.append_text(line2)
        return line1


class WorkerListPanel(Static):
    can_focus = True

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.rows: list[dict] = []
        self.selected_worker_id: Optional[str] = None

    def update_workers(self, rows: list[dict], selected_worker_id: Optional[str]) -> None:
        self.rows = rows
        self.selected_worker_id = selected_worker_id
        self.refresh()

    def render(self) -> Text:
        text = Text()
        text.append("Workers\n", style="bold")
        text.append("\n")
        if not self.rows:
            text.append("  -\n", style="dim")
            return text
        for row in self.rows:
            marker = "> " if row.get("worker_id") == self.selected_worker_id else "  "
            style = "reverse" if row.get("worker_id") == self.selected_worker_id else "white"
            elapsed = int(row.get("elapsed", 0) or 0)
            line = (
                f"{marker}{row.get('worker_id', '?'):<3} "
                f"{row.get('state', '-'):<6} "
                f"{_shorten(row.get('step', '-') or '-', 14):<14} "
                f"{elapsed:>3}s"
            )
            text.append(f"{line}\n", style=style)
            account = row.get("account") or "-"
            text.append(f"    {_shorten(account, 26)}\n", style="dim")
        text.append("\n")
        text.append("↑/↓ select\n", style="dim")
        text.append("Enter inspect\n", style="dim")
        text.append("Esc back", style="dim")
        return text


class PoolStatsPanel(Static):
    can_focus = True

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.snapshot: dict = {}

    def update_snapshot(self, snapshot: Optional[dict]) -> None:
        self.snapshot = snapshot or {}
        self.refresh()

    def render(self):
        stats = self.snapshot or {}
        table = Table.grid(padding=(0, 1))
        table.add_row("[b]Pool Stats[/b]", "")
        table.add_row("", "")
        table.add_row("[b]Config[/b]", "")
        table.add_row("max_reuse", str(stats.get("max_reuse", 0)))
        table.add_row("max_active", str(stats.get("max_active", 0)))
        table.add_row("lease_sec", str(stats.get("lease_seconds", 0)))
        table.add_row("", "")
        table.add_row("[b]Counters[/b]", "")
        table.add_row("fresh", str(stats.get("fresh_total", 0)))
        table.add_row("reuse", str(stats.get("reuse_total", 0)))
        table.add_row("reuse rate", f"{float(stats.get('reuse_rate', 0.0)) * 100:.1f}%")
        table.add_row("spent", f"${float(stats.get('spent', 0.0)):.4f}")
        table.add_row("waiters", str(stats.get("cap_waiters", 0)))
        table.add_row("", "")
        table.add_row("[b]Active Leases[/b]", f"{stats.get('active', 0)}/{stats.get('max_active', 0)}")
        leases = list(_format_leases(stats.get("leases") or []))
        if leases:
            for lease in leases:
                table.add_row("", lease)
        else:
            table.add_row("", "-")
        return table


def _format_leases(leases: Iterable[dict]) -> Iterable[str]:
    for lease in leases:
        worker = lease.get("worker_id") or "?"
        phone = lease.get("phone_number") or "?"
        used = lease.get("used_count", 0)
        max_reuse = lease.get("max_reuse", 0)
        origin = "R" if lease.get("is_reused") else "F"
        yield f"{worker} {phone} {used}/{max_reuse} {origin}"


def _shorten(value: str, width: int) -> str:
    value = str(value or "")
    if len(value) <= width:
        return value
    if width <= 3:
        return value[:width]
    return value[: width - 3] + "..."
