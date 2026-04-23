from __future__ import annotations

import contextlib
import threading
import time
import warnings
from typing import Any, Callable, Optional

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer, Header, RichLog, TabbedContent, TabPane

from . import bus
from .fallback import StreamCapture
from .render import format_event_text
from .widgets import PoolStatsPanel, StatusBar, WorkerPanel


class RegisterMonitorApp(App):
    CSS = """
    #workers {
        height: 12;
    }
    WorkerPanel {
        width: 1fr;
        border: solid #666666;
        margin-right: 1;
    }
    #middle {
        height: 1fr;
    }
    #features {
        width: 2fr;
    }
    RichLog {
        border: solid #444444;
        margin-bottom: 1;
    }
    #pool-stats {
        width: 1fr;
        border: solid #666666;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("ctrl+c", "quit", show=False),
        Binding("p", "pause_intake", "Pause"),
        Binding("r", "resume_intake", "Resume"),
        Binding("f", "cycle_filter", "Filter"),
        Binding("1", "focus_worker('W1')", "W1", show=False),
        Binding("2", "focus_worker('W2')", "W2", show=False),
        Binding("3", "focus_worker('W3')", "W3", show=False),
        Binding("4", "focus_worker('W4')", "W4", show=False),
        Binding("5", "focus_worker('W5')", "W5", show=False),
        Binding("6", "focus_worker('W6')", "W6", show=False),
        Binding("7", "focus_worker('W7')", "W7", show=False),
        Binding("8", "focus_worker('W8')", "W8", show=False),
        Binding("9", "focus_worker('W9')", "W9", show=False),
    ]

    def __init__(
        self,
        run_callable: Callable[[], Any],
        *,
        max_workers: int,
        pool_getter: Optional[Callable[[], Any]] = None,
        summary_getter: Optional[Callable[[], dict[str, Any]]] = None,
        inflight_getter: Optional[Callable[[], Any]] = None,
        intake_paused: Optional[threading.Event] = None,
        shutdown_event: Optional[threading.Event] = None,
        quit_grace_seconds: float = 120.0,
    ):
        super().__init__()
        self.run_callable = run_callable
        self.max_workers = max_workers
        self.pool_getter = pool_getter
        self.summary_getter = summary_getter
        self.inflight_getter = inflight_getter
        self.intake_paused = intake_paused or threading.Event()
        self.shutdown_event = shutdown_event or threading.Event()
        self.quit_grace_seconds = quit_grace_seconds
        self._bus_queue = bus.subscribe(maxsize=8192)
        self._runner = None
        self._started_at = time.time()
        self._worker_panels: dict[str, WorkerPanel] = {}
        self._run_result: Any = None
        self._run_error: Optional[BaseException] = None
        self._captured_stdout = StreamCapture("system")
        self._captured_stderr = StreamCapture("system", level="warn")
        self._stdout_cm = None
        self._stderr_cm = None
        self._warnings_cm = None
        self._warn_events = 0
        self._completed = False
        self._cleanup_done = False
        self._active_workers: set[str] = set()

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield StatusBar(id="status")
        with Horizontal(id="workers"):
            for i in range(self.max_workers):
                worker_id = f"W{i + 1}"
                panel = WorkerPanel(worker_id, id=f"worker-{worker_id.lower()}")
                self._worker_panels[worker_id] = panel
                yield panel
        with Horizontal(id="middle"):
            with Vertical(id="features"):
                yield RichLog(id="log-sentinel", max_lines=2000)
                yield RichLog(id="log-email", max_lines=2000)
                yield RichLog(id="log-sms", max_lines=2000)
                yield RichLog(id="log-phone_pool", max_lines=2000)
                yield RichLog(id="log-system", max_lines=2000)
            yield PoolStatsPanel(id="pool-stats")
        with TabbedContent(id="all-logs"):
            with TabPane("All", id="tab-all"):
                yield RichLog(id="log-all", max_lines=5000)
            with TabPane("✓", id="tab-success"):
                yield RichLog(id="log-success", max_lines=2000)
            with TabPane("✗", id="tab-fail"):
                yield RichLog(id="log-fail", max_lines=2000)
            with TabPane("⚠", id="tab-warn"):
                yield RichLog(id="log-warn", max_lines=2000)
        yield Footer()

    def on_mount(self) -> None:
        self._install_stream_capture()
        self.set_interval(0.1, self._drain_bus)
        self.set_interval(1.0, self._tick_status)
        self.set_interval(1.0, self._refresh_pool_stats)
        self._runner = threading.Thread(
            target=self._kickoff_workers,
            name="register-monitor-runner",
            daemon=True,
        )
        self._runner.start()

    def on_unmount(self) -> None:
        self._restore_stream_capture()
        self._cleanup_bus()

    def _install_stream_capture(self) -> None:
        self._stdout_cm = contextlib.redirect_stdout(self._captured_stdout)
        self._stderr_cm = contextlib.redirect_stderr(self._captured_stderr)
        self._warnings_cm = warnings.catch_warnings(record=False)
        self._stdout_cm.__enter__()
        self._stderr_cm.__enter__()
        self._warnings_cm.__enter__()
        warnings.simplefilter("default")
        warnings.showwarning = self._showwarning

    def _restore_stream_capture(self) -> None:
        self._captured_stdout.flush()
        self._captured_stderr.flush()
        if self._warnings_cm is not None:
            self._warnings_cm.__exit__(None, None, None)
            self._warnings_cm = None
        if self._stderr_cm is not None:
            self._stderr_cm.__exit__(None, None, None)
            self._stderr_cm = None
        if self._stdout_cm is not None:
            self._stdout_cm.__exit__(None, None, None)
            self._stdout_cm = None

    def _showwarning(self, message, category, filename, lineno, file=None, line=None) -> None:
        bus.emit("system", warnings.formatwarning(message, category, filename, lineno, line).strip(), level="warn")

    def _kickoff_workers(self) -> None:
        try:
            self._run_result = self.run_callable()
        except BaseException as exc:
            self._run_error = exc
        finally:
            try:
                self.call_from_thread(self._finish_run)
            except Exception:
                self._finish_run()

    def _finish_run(self) -> None:
        self._drain_bus()
        self._restore_stream_capture()
        if self._run_error is not None:
            bus.emit("system", f"Run failed: {self._run_error}", level="error")
            self._drain_bus()
        self._completed = True
        self._sync_worker_activity()
        self._tick_status()

    def _tick_status(self) -> None:
        status = self.query_one("#status", StatusBar)
        status.max_workers = self.max_workers
        status.uptime_seconds = time.time() - self._started_at
        summary = self.summary_getter() if self.summary_getter else {}
        if summary:
            status.done = int(summary.get("done", status.done))
            status.success = int(summary.get("success", status.success))
            status.fail = int(summary.get("fail", status.fail))
        status.warn = self._warn_events
        self._sync_worker_activity()
        elapsed_minutes = max(status.uptime_seconds / 60.0, 1e-6)
        status.rate = status.done / elapsed_minutes
        status.dropped = int(bus.stats().get("dropped_events", 0))
        status.paused = self.intake_paused.is_set()
        status.run_state = "failed" if self._run_error is not None else ("completed" if self._completed else "running")
        status.status_hint = "press q to quit" if self._completed else ""

    def _refresh_pool_stats(self) -> None:
        if not self.pool_getter:
            return
        pool = self.pool_getter()
        if pool is None or not hasattr(pool, "stats"):
            return
        snapshot = pool.stats()
        self.query_one("#pool-stats", PoolStatsPanel).update_snapshot(snapshot)

    def _drain_bus(self) -> None:
        for _ in range(200):
            try:
                event = self._bus_queue.get_nowait()
            except Exception:
                break
            plain_line = self._format_event(event)
            rich_line = format_event_text(event)
            self._write_log("all", rich_line)
            if event.channel in {"sentinel", "email", "sms", "phone_pool", "system"}:
                self._write_log(event.channel, rich_line)
            if event.level == "success":
                self._write_log("success", rich_line)
            elif event.level == "error":
                self._write_log("fail", rich_line)
            elif event.level == "warn":
                self._write_log("warn", rich_line)
                self._warn_events += 1
            if event.worker_id:
                panel = self._worker_panels.get(event.worker_id)
                if panel is not None:
                    if event.fields.get("account"):
                        panel.set_account(event.fields.get("account"))
                    if event.fields.get("step"):
                        panel.set_step(event.fields.get("step"))
                    panel.add_log(plain_line)
        self._sync_worker_activity()

    def _sync_worker_activity(self) -> None:
        status = self.query_one("#status", StatusBar)
        next_active = self._get_inflight_workers()
        for worker_id in self._active_workers - next_active:
            panel = self._worker_panels.get(worker_id)
            if panel is not None:
                panel.mark_idle()
        if self._completed:
            for worker_id, panel in self._worker_panels.items():
                if worker_id not in next_active and panel.state == "active":
                    panel.mark_idle()
        self._active_workers = next_active
        status.active_workers = len(next_active)

    def _get_inflight_workers(self) -> set[str]:
        if not self.inflight_getter:
            return set()
        try:
            workers = self.inflight_getter() or []
        except Exception:
            return set()
        return {str(worker_id) for worker_id in workers if worker_id}

    def _write_log(self, channel_name: str, line) -> None:
        try:
            log = self.query_one(f"#log-{channel_name}", RichLog)
        except Exception:
            return
        log.write(line)

    @staticmethod
    def _format_event(event) -> str:
        stamp = time.strftime("%H:%M:%S", time.localtime(event.ts))
        worker = f"[{event.worker_id}]" if event.worker_id else ""
        return f"[{stamp}][{event.channel}]{worker} {event.msg}"

    def action_pause_intake(self) -> None:
        self.intake_paused.set()

    def action_resume_intake(self) -> None:
        self.intake_paused.clear()

    def action_cycle_filter(self) -> None:
        tabs = self.query_one("#all-logs", TabbedContent)
        order = ["tab-all", "tab-success", "tab-fail", "tab-warn"]
        current = tabs.active or "tab-all"
        index = order.index(current) if current in order else 0
        tabs.active = order[(index + 1) % len(order)]

    def action_focus_worker(self, worker_id: str) -> None:
        panel = self._worker_panels.get(worker_id)
        if panel is not None:
            panel.focus()

    def action_quit(self) -> None:
        if not self._completed:
            self.shutdown_event.set()
        self._drain_bus()
        self._restore_stream_capture()
        self._cleanup_bus()
        self.exit(self._run_result)

    def _cleanup_bus(self) -> None:
        if self._cleanup_done:
            return
        bus.unsubscribe(self._bus_queue)
        self._cleanup_done = True
