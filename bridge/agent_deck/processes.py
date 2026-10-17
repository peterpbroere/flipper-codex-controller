"""Process identity includes creation time, so reused PIDs are not sessions."""
from __future__ import annotations

import os
import re

import psutil


def _process_kind(process):
    """A server's lifetime cannot establish whether a client session is open."""
    try:
        args = process.cmdline()
        if not args:
            return "unknown"
        return "shared" if "app-server" in args[1:] else "session"
    except (psutil.Error, OSError):
        return "unknown"


def classify_owner(pid: int, created: float):
    """Classify old journal entries without trusting a reused process ID."""
    if not pid or not created:
        return "unknown"
    try:
        process = psutil.Process(pid)
        if abs(process.create_time() - created) >= 0.01:
            return "unknown"
        if process.name().lower() not in {"codex", "codex.exe"}:
            return "unknown"
        return _process_kind(process)
    except (psutil.Error, OSError):
        return "unknown"


def owner_context():
    context = {"owner_pid": 0, "owner_created": 0, "owner_kind": "unknown", "tab": ""}
    tab = os.environ.get("AGENT_DECK_TAB", "")
    if re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", tab):
        context["tab"] = tab
    try:
        for process in psutil.Process().parents():
            try:
                if process.name().lower() not in {"codex", "codex.exe"}:
                    continue
                candidate = {"owner_pid": process.pid, "owner_created": process.create_time(),
                             "owner_kind": _process_kind(process)}
                # A dedicated CLI can own an app-server child. Only an explicit
                # --no-daemon ancestor may replace an already-found server:
                # the CLI that originally launched a shared daemon may exit first.
                if candidate["owner_kind"] == "session" and (
                        not context["owner_pid"] or "--no-daemon" in process.cmdline()[1:]):
                    context.update(candidate)
                    break
                if not context["owner_pid"]:
                    context.update(candidate)
            except (psutil.Error, OSError):
                continue
    except (psutil.Error, OSError):
        pass
    return context


def is_alive(pid: int, created: float):
    if not pid or not created:
        return None
    try:
        process = psutil.Process(pid)
        return abs(process.create_time() - created) < 0.01 and process.is_running()
    except psutil.AccessDenied:
        return None
    except (psutil.Error, OSError):
        return False
