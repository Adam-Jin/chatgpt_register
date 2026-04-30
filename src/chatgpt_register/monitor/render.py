from __future__ import annotations

from datetime import datetime

from rich.text import Text

from .bus import Event


LEVEL_STYLES = {
    "info": "white",
    "success": "bold green",
    "warn": "bold yellow",
    "error": "bold red",
}

LEVEL_ANSI = {
    "info": "\033[37m",
    "success": "\033[1;32m",
    "warn": "\033[1;33m",
    "error": "\033[1;31m",
}

CHANNEL_STYLE = "cyan"
WORKER_STYLE = "magenta"
STAMP_STYLE = "dim"
MESSAGE_STYLE = "white"
RESET_ANSI = "\033[0m"
STAMP_ANSI = "\033[2m"
CHANNEL_ANSI = "\033[36m"
WORKER_ANSI = "\033[35m"
MESSAGE_ANSI = "\033[37m"


def format_event_plain(event: Event) -> str:
    ts = datetime.fromtimestamp(event.ts).strftime("%Y-%m-%d %H:%M:%S")
    worker = f"[{event.worker_id}]" if event.worker_id else ""
    return f"{ts} [{event.level.upper()}][{event.channel}]{worker} {event.msg}"


def format_event_text(event: Event) -> Text:
    line = Text()
    stamp = datetime.fromtimestamp(event.ts).strftime("%H:%M:%S")
    line.append(f"[{stamp}]", style=STAMP_STYLE)
    line.append(f"[{event.level.upper()}]", style=LEVEL_STYLES.get(event.level, "white"))
    line.append(f"[{event.channel}]", style=CHANNEL_STYLE)
    if event.worker_id:
        line.append(f"[{event.worker_id}]", style=WORKER_STYLE)
    line.append(" ")
    line.append(event.msg, style=MESSAGE_STYLE)
    return line


def colorize_plain_event(event: Event) -> str:
    ts = datetime.fromtimestamp(event.ts).strftime("%Y-%m-%d %H:%M:%S")
    level = event.level.upper()
    worker = f"{WORKER_ANSI}[{event.worker_id}]{RESET_ANSI}" if event.worker_id else ""
    return (
        f"{STAMP_ANSI}{ts}{RESET_ANSI} "
        f"{LEVEL_ANSI.get(event.level, LEVEL_ANSI['info'])}[{level}]{RESET_ANSI}"
        f"{CHANNEL_ANSI}[{event.channel}]{RESET_ANSI}"
        f"{worker} "
        f"{MESSAGE_ANSI}{event.msg}{RESET_ANSI}"
    )
