"""Paths that remain stable in source checkouts and packaged builds."""
from __future__ import annotations

import os
from pathlib import Path
import sys


SOURCE_ROOT = Path(__file__).resolve().parents[2]
PRODUCT_STATE_DIR = "FlipperAgentDeck"


def is_frozen():
    return bool(getattr(sys, "frozen", False))


def app_root():
    return Path(sys.executable).resolve().parent if is_frozen() else SOURCE_ROOT


def resource_root():
    return Path(getattr(sys, "_MEIPASS", app_root()))


def state_dir():
    configured = os.environ.get("AGENT_DECK_STATE_DIR")
    if configured:
        return Path(configured).expanduser()
    if is_frozen():
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local/share"))
        return base / PRODUCT_STATE_DIR
    return SOURCE_ROOT / ".agent-deck"


def bridge_executable():
    """Return the console executable used for bridge child and hook processes."""
    executable = Path(sys.executable).resolve()
    if is_frozen():
        # The GUI entry point may be windowed; use its console sibling for the
        # bridge and Codex hooks so stdin/stdout and diagnostics remain available.
        console_name = "AgentDeck.exe" if os.name == "nt" else "AgentDeck"
        candidate = executable.with_name(console_name)
        if candidate.is_file():
            return candidate
    return executable
