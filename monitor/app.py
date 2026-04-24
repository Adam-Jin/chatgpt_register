from __future__ import annotations

import contextlib
from contextlib import contextmanager
import threading
import time
import warnings
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Optional

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer, Header, RichLog, Static

from . import bus
from .bus import Event
from .fallback import StreamCapture
from .render import format_event_text
from .widgets import PoolStatsPanel, StatusBar, WorkerListPanel


VIEW_LOGS = "logs"
VIEW_WORKERS = "workers"
VIEW_POOL = "pool"
VIEW_WORKER_DETAIL = "worker_detail"

FILTER_ORDER = ["all", "success", "fail", "warn"]
FILTER_LABELS = {
    "all": "All Logs",
    "success": "Success Logs",
    "fail": "Failed Logs",
    "warn": "Warnings",
}


@dataclass
class WorkerState:
    worker_id: str
    account: str = "-"
    step: str = "-"
    state: str = "idle"
    started_at: Optional[float] = None
    finished_at: Optional[float] = None

    def set_account(self, account: Optional[str]) -> None:
        self.account = str(account or "-")
        self._mark_active()

    def set_step(self, step: Optional[str]) -> None:
        self.step = str(step or "-")
        self._mark_active()

    def mark_active(self) -> None:
        self._mark_active()

    def mark_idle(self) -> None:
        if self.state == "active":
            self.finished_at = time.time()
        self.state = "idle"
        if self.step == "-":
            self.step = "done"

    def elapsed_seconds(self) -> int:
        if self.started_at is None:
            return 0
        end_ts = self.finished_at if self.finished_at is not None else time.time()
        return int(max(0, end_ts - self.started_at))

    def _mark_active(self) -> None:
        if self.state != "active":
            self.started_at = time.time()
            self.finished_at = None
        self.state = "active"


class MonitorRichLog(RichLog):
    def __init__(self, *args, on_scroll_change: Optional[Callable[[RichLog, float, float], None]] = None, **kwargs):
        super().__init__(*args, **kwargs)
        self._on_scroll_change = on_scroll_change

    def watch_scroll_y(self, old_value: float, new_value: float) -> None:
        super().watch_scroll_y(old_value, new_value)
        if self._on_scroll_change is not None:
            self._on_scroll_change(self, old_value, new_value)


class RegisterMonitorApp(App):
    CSS = """
    .hidden {
        display: none;
    }
    #content {
        height: 1fr;
    }
    #worker-list {
        width: 34;
        border: solid #666666;
        margin-right: 1;
        padding: 0 1;
    }
    #log-pane {
        width: 1fr;
    }
    #log-title {
        height: 1;
        padding: 0 1;
    }
    #main-log {
        height: 1fr;
        border: solid #444444;
    }
    #pool-stats {
        width: 34;
        border: solid #666666;
        margin-left: 1;
        padding: 0 1;
    }
    #quit-hint {
        height: 1;
        padding: 0 1;
        content-align: right middle;
        color: $warning;
        background: $surface;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit", priority=True),
        Binding("Q", "quit", show=False, priority=True),
        Binding("ctrl+c", "interrupt_quit", show=False, priority=True),
        Binding("p", "pause_intake", "Pause", priority=True),
        Binding("P", "pause_intake", show=False, priority=True),
        Binding("r", "resume_intake", "Resume", priority=True),
        Binding("R", "resume_intake", show=False, priority=True),
        Binding("f", "cycle_filter", "Filter", priority=True),
        Binding("F", "cycle_filter", show=False, priority=True),
        Binding("pageup", "log_page_up", "PgUp", show=False, priority=True),
        Binding("pagedown", "log_page_down", "PgDn", show=False, priority=True),
        Binding("home", "log_home", "Home", show=False, priority=True),
        Binding("end", "log_end", "End", show=False, priority=True),
        Binding("w", "toggle_worker_list", "Workers", priority=True),
        Binding("W", "toggle_worker_list", show=False, priority=True),
        Binding("s", "toggle_pool_stats", "Pool", priority=True),
        Binding("S", "toggle_pool_stats", show=False, priority=True),
        Binding("escape", "back", "Back", priority=True),
        Binding("enter", "inspect_selected_worker", "Inspect", priority=True),
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
        self._view_mode = VIEW_LOGS
        self._filter_mode = "all"
        self._pool_snapshot: dict[str, Any] = {}
        self._all_history: deque[Event] = deque(maxlen=6000)
        self._worker_history: dict[str, deque[Event]] = {}
        self._worker_states = {f"W{i + 1}": WorkerState(worker_id=f"W{i + 1}") for i in range(max_workers)}
        self._selected_worker_id = next(iter(self._worker_states), None)
        self._follow_logs = True
        self._suspend_follow_tracking = 0
        self._quit_armed_until = 0.0
        self._quit_hint = ""

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield StatusBar(id="status")
        with Horizontal(id="content"):
            yield WorkerListPanel(id="worker-list", classes="hidden")
            with Vertical(id="log-pane"):
                yield Static("All Logs", id="log-title")
                yield MonitorRichLog(id="main-log", max_lines=6000, on_scroll_change=self._handle_log_scroll_change)
            yield PoolStatsPanel(id="pool-stats", classes="hidden")
        yield Static("", id="quit-hint", classes="hidden")
        yield Footer()

    def on_mount(self) -> None:
        self._install_stream_capture()
        self._refresh_layout()
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

    def on_key(self, event) -> None:
        if self._view_mode == VIEW_WORKERS:
            if event.key == "up":
                self._move_worker_selection(-1)
                event.stop()
            elif event.key == "down":
                self._move_worker_selection(1)
                event.stop()
            return
        if not self._is_log_focused():
            return
        if event.key in {"up", "pageup", "home"}:
            self._set_follow_logs(False)
        elif event.key == "end":
            self._set_follow_logs(True)

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
        if self._quit_armed_until and time.time() > self._quit_armed_until:
            self._clear_quit_hint()
        status.status_hint = self._quit_hint or ("press q to quit" if self._completed else "")

        status.pool_fresh = int(self._pool_snapshot.get("fresh_total", 0))
        status.pool_reuse = int(self._pool_snapshot.get("reuse_total", 0))
        status.pool_active = int(self._pool_snapshot.get("active", 0))
        status.pool_max_active = int(self._pool_snapshot.get("max_active", 0))
        status.pool_waiters = int(self._pool_snapshot.get("cap_waiters", 0))
        status.pool_spent = float(self._pool_snapshot.get("spent", 0.0))

        if self._view_mode == VIEW_WORKER_DETAIL and self._selected_worker_id:
            state = self._worker_states.get(self._selected_worker_id)
            if state is not None:
                status.viewing_worker = state.worker_id
                status.viewing_state = state.state
                status.viewing_step = state.step
                status.viewing_elapsed = state.elapsed_seconds()
        else:
            status.viewing_worker = ""
            status.viewing_state = ""
            status.viewing_step = ""
            status.viewing_elapsed = 0

        self._render_worker_list()

    def _refresh_pool_stats(self) -> None:
        if not self.pool_getter:
            return
        pool = self.pool_getter()
        if pool is None or not hasattr(pool, "stats"):
            return
        snapshot = dict(pool.stats() or {})
        snapshot.setdefault("max_reuse", int(getattr(pool, "max_reuse", 0) or 0))
        snapshot.setdefault("lease_seconds", int(getattr(pool, "lease_seconds", 0) or 0))
        snapshot.setdefault("max_active", int(getattr(pool, "max_active", 0) or 0))
        self._pool_snapshot = snapshot
        self.query_one("#pool-stats", PoolStatsPanel).update_snapshot(snapshot)
        self._tick_status()

    def _drain_bus(self) -> None:
        for _ in range(200):
            try:
                event = self._bus_queue.get_nowait()
            except Exception:
                break
            self._record_event(event)
            self._update_worker_from_event(event)
            if event.level == "warn":
                self._warn_events += 1
            self._append_event_if_visible(event)
        self._sync_worker_activity()
        self._render_worker_list()

    def _record_event(self, event: Event) -> None:
        self._all_history.append(event)
        if event.worker_id:
            self._worker_history.setdefault(event.worker_id, deque(maxlen=3000)).append(event)

    def _update_worker_from_event(self, event: Event) -> None:
        if not event.worker_id:
            return
        state = self._worker_states.setdefault(event.worker_id, WorkerState(worker_id=event.worker_id))
        state.mark_active()
        if event.fields.get("account"):
            state.set_account(event.fields.get("account"))
        if event.fields.get("step"):
            state.set_step(event.fields.get("step"))

    def _sync_worker_activity(self) -> None:
        try:
            status = self.query_one("#status", StatusBar)
        except Exception:
            return
        next_active = self._get_inflight_workers()
        for worker_id in self._active_workers - next_active:
            state = self._worker_states.get(worker_id)
            if state is not None:
                state.mark_idle()
        if self._completed:
            for worker_id, state in self._worker_states.items():
                if worker_id not in next_active and state.state == "active":
                    state.mark_idle()
        for worker_id in next_active:
            state = self._worker_states.get(worker_id)
            if state is not None:
                state.mark_active()
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

    def _append_event_if_visible(self, event: Event) -> None:
        if not self._follow_logs:
            return
        if self._view_mode == VIEW_WORKER_DETAIL:
            if event.worker_id == self._selected_worker_id:
                self._write_log(self._format_detail_event_text(event))
            return
        if self._event_matches_filter(event):
            self._write_log(format_event_text(event))

    def _refresh_layout(self) -> None:
        worker_list = self.query_one("#worker-list", WorkerListPanel)
        pool_panel = self.query_one("#pool-stats", PoolStatsPanel)

        worker_list.display = self._view_mode == VIEW_WORKERS
        pool_panel.display = self._view_mode == VIEW_POOL
        self._update_log_title()

        self._render_worker_list()
        self._render_current_log()
        self._focus_active_view()

    def _render_current_log(self) -> None:
        log = self.query_one("#main-log", RichLog)
        with self._suspend_log_follow_tracking():
            log.clear()
            for event in self._iter_visible_events():
                if self._view_mode == VIEW_WORKER_DETAIL:
                    log.write(self._format_detail_event_text(event))
                else:
                    log.write(format_event_text(event))
            if self._follow_logs:
                self._scroll_log_end()

    def _iter_visible_events(self) -> Iterable[Event]:
        if self._view_mode == VIEW_WORKER_DETAIL and self._selected_worker_id:
            history = self._worker_history.get(self._selected_worker_id, ())
            for event in history:
                yield event
            return
        for event in self._all_history:
            if self._event_matches_filter(event):
                yield event

    def _render_worker_list(self) -> None:
        rows = []
        worker_ids = list(self._worker_states.keys())
        if not self._selected_worker_id and worker_ids:
            self._selected_worker_id = worker_ids[0]
        for worker_id in worker_ids:
            state = self._worker_states[worker_id]
            rows.append({
                "worker_id": worker_id,
                "state": state.state,
                "step": state.step,
                "account": state.account,
                "elapsed": state.elapsed_seconds(),
            })
        self.query_one("#worker-list", WorkerListPanel).update_workers(rows, self._selected_worker_id)

    def _move_worker_selection(self, delta: int) -> None:
        worker_ids = list(self._worker_states.keys())
        if not worker_ids:
            return
        if self._selected_worker_id not in worker_ids:
            self._selected_worker_id = worker_ids[0]
        index = worker_ids.index(self._selected_worker_id)
        index = max(0, min(len(worker_ids) - 1, index + delta))
        self._selected_worker_id = worker_ids[index]
        self._render_worker_list()

    def _current_log_title(self) -> str:
        mode = "FOLLOW" if self._follow_logs else "REVIEW"
        if self._view_mode == VIEW_WORKER_DETAIL and self._selected_worker_id:
            state = self._worker_states.get(self._selected_worker_id)
            if state is None:
                return f"{self._selected_worker_id} Logs | {mode}"
            return (
                f"{state.worker_id} | {state.state} | acct: {state.account} | "
                f"step: {state.step} | elapsed: {state.elapsed_seconds()}s | {mode}"
            )
        return f"{FILTER_LABELS.get(self._filter_mode, 'All Logs')} | {mode}"

    def _event_matches_filter(self, event: Event) -> bool:
        if self._filter_mode == "all":
            return True
        if self._filter_mode == "success":
            return event.level == "success"
        if self._filter_mode == "fail":
            return event.level == "error"
        if self._filter_mode == "warn":
            return event.level == "warn"
        return True

    def _write_log(self, line) -> None:
        log = self.query_one("#main-log", RichLog)
        with self._suspend_log_follow_tracking():
            log.write(line)
            if self._follow_logs:
                self._scroll_log_end()

    def _update_log_title(self) -> None:
        self.query_one("#log-title", Static).update(self._current_log_title())

    def _set_follow_logs(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if self._follow_logs == enabled:
            return
        self._follow_logs = enabled
        self._update_log_title()

    def _scroll_log_end(self) -> None:
        self._invoke_log_method("scroll_end", "action_end")

    def _is_log_focused(self) -> bool:
        with contextlib.suppress(Exception):
            log = self.query_one("#main-log", RichLog)
            focused = self.focused
            return focused is log or log in getattr(focused, "ancestors", ())
        return False

    def _handle_log_scroll_change(self, log: RichLog, _old_value: float, new_value: float) -> None:
        if self._suspend_follow_tracking or self._view_mode not in {VIEW_LOGS, VIEW_WORKER_DETAIL}:
            return
        if not self._is_log_at_end(log, new_value):
            self._set_follow_logs(False)

    def _is_log_at_end(self, log: Optional[RichLog] = None, scroll_y: Optional[float] = None) -> bool:
        if log is None:
            log = self.query_one("#main-log", RichLog)
        current_scroll_y = float(log.scroll_y if scroll_y is None else scroll_y)
        return abs(float(log.max_scroll_y) - current_scroll_y) <= 1.0

    @contextmanager
    def _suspend_log_follow_tracking(self):
        self._suspend_follow_tracking += 1
        try:
            yield
        finally:
            self._suspend_follow_tracking = max(0, self._suspend_follow_tracking - 1)

    def _invoke_log_method(self, *method_names: str) -> None:
        log = self.query_one("#main-log", RichLog)
        for method_name in method_names:
            method = getattr(log, method_name, None)
            if not callable(method):
                continue
            for kwargs in (
                {"animate": False, "immediate": True},
                {"animate": False},
                {"immediate": True},
                {},
            ):
                with contextlib.suppress(TypeError, Exception):
                    method(**kwargs)
                    return
            with contextlib.suppress(TypeError):
                method()
                return
            with contextlib.suppress(TypeError):
                method(animate=False)
                return
            with contextlib.suppress(Exception):
                method(False)
                return

    def _focus_active_view(self) -> None:
        if self._view_mode == VIEW_WORKERS:
            self.query_one("#worker-list", WorkerListPanel).focus()
            return
        if self._view_mode == VIEW_POOL:
            self.query_one("#pool-stats", PoolStatsPanel).focus()
            return
        self.query_one("#main-log", RichLog).focus()

    @staticmethod
    def _format_detail_event_text(event: Event) -> Text:
        line = Text()
        stamp = time.strftime("%H:%M:%S", time.localtime(event.ts))
        line.append(f"[{stamp}]", style="dim")
        if event.channel != "worker":
            line.append(f"[{event.channel}]", style="cyan")
        if event.level != "info":
            level_style = {
                "success": "bold green",
                "warn": "bold yellow",
                "error": "bold red",
            }.get(event.level, "white")
            line.append(f"[{event.level.upper()}]", style=level_style)
        line.append(" ")
        line.append(event.msg, style="white")
        return line

    def action_pause_intake(self) -> None:
        self.intake_paused.set()

    def action_resume_intake(self) -> None:
        self.intake_paused.clear()

    def action_cycle_filter(self) -> None:
        if self._view_mode == VIEW_WORKER_DETAIL:
            return
        current = FILTER_ORDER.index(self._filter_mode) if self._filter_mode in FILTER_ORDER else 0
        self._filter_mode = FILTER_ORDER[(current + 1) % len(FILTER_ORDER)]
        self._refresh_layout()

    def action_log_page_up(self) -> None:
        self._set_follow_logs(False)
        self._invoke_log_method("scroll_page_up", "action_page_up")

    def action_log_page_down(self) -> None:
        self._invoke_log_method("scroll_page_down", "action_page_down")

    def action_log_home(self) -> None:
        self._set_follow_logs(False)
        self._invoke_log_method("scroll_home", "action_home")

    def action_log_end(self) -> None:
        self._set_follow_logs(True)
        self._render_current_log()

    def action_toggle_worker_list(self) -> None:
        if self._view_mode == VIEW_WORKERS:
            self._view_mode = VIEW_LOGS
        else:
            self._view_mode = VIEW_WORKERS
        self._refresh_layout()

    def action_toggle_pool_stats(self) -> None:
        if self._view_mode == VIEW_WORKER_DETAIL:
            return
        self._view_mode = VIEW_LOGS if self._view_mode == VIEW_POOL else VIEW_POOL
        self._refresh_layout()

    def action_back(self) -> None:
        if self._view_mode == VIEW_LOGS:
            return
        self._view_mode = VIEW_LOGS
        self._refresh_layout()

    def action_inspect_selected_worker(self) -> None:
        if self._view_mode != VIEW_WORKERS or not self._selected_worker_id:
            return
        self._view_mode = VIEW_WORKER_DETAIL
        self._refresh_layout()

    def action_quit(self) -> None:
        if not self._completed:
            self.shutdown_event.set()
        self._drain_bus()
        self._restore_stream_capture()
        self._cleanup_bus()
        self.exit(self._run_result)

    def action_interrupt_quit(self) -> None:
        now = time.time()
        if self._quit_armed_until and now <= self._quit_armed_until:
            self.action_quit()
            return
        self._quit_armed_until = now + 2.0
        self._show_quit_hint("Press Ctrl+C again to quit")
        self._tick_status()

    def _cleanup_bus(self) -> None:
        if self._cleanup_done:
            return
        bus.unsubscribe(self._bus_queue)
        self._cleanup_done = True

    def _show_quit_hint(self, message: str) -> None:
        self._quit_hint = message
        hint = self.query_one("#quit-hint", Static)
        hint.update(message)
        hint.display = True

    def _clear_quit_hint(self) -> None:
        self._quit_armed_until = 0.0
        self._quit_hint = ""
        hint = self.query_one("#quit-hint", Static)
        hint.update("")
        hint.display = False
