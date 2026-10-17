"""Merge our observational hooks without replacing other integrations."""
from __future__ import annotations

import copy
from datetime import datetime
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

from .hook import EVENTS


def merged_hooks(existing, command):
    result = copy.deepcopy(existing)
    hooks = result.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("Existing hooks must be an object")
    for event in sorted(EVENTS):
        groups = hooks.setdefault(event, [])
        if not isinstance(groups, list):
            raise ValueError(f"Existing {event} hooks must be a list")
        if any(any(h.get("commandWindows", h.get("command")) == command for h in g.get("hooks", [])) for g in groups):
            continue
        hook = {"type": "command", "command": command, "timeout": 3}
        if os.name == "nt":
            hook["commandWindows"] = command
        group = {"hooks": [hook]}
        if event == "PreToolUse":
            group["matcher"] = "request_user_input"
        groups.append(group)
    return result


def install_hooks(path=None, apply=False):
    config = Path(path).expanduser() if path else Path.home() / ".codex/hooks.json"
    script = Path(__file__).resolve().parents[2] / "scripts/codex-hook.py"
    if not script.is_file():
        raise ValueError("Run hook setup from the source checkout")
    argv = [sys.executable, str(script)]
    command = subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)
    existing = json.loads(config.read_text(encoding="utf-8-sig")) if config.exists() else {}
    merged = merged_hooks(existing, command)
    if existing == merged:
        print(f"Agent Deck hooks are already registered in {config}")
        return
    text = json.dumps(merged, indent=2) + "\n"
    if not apply:
        print(text)
        print("Preview only. Add --apply to save. Existing hooks will be backed up and preserved.")
        return
    config.parent.mkdir(parents=True, exist_ok=True)
    if config.exists():
        backup = config.with_name(config.name + ".agent-deck-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f") + ".bak")
        shutil.copy2(config, backup)
        print(f"Previous hooks saved to {backup}")
    temp = config.with_name(config.name + ".agent-deck.tmp")
    temp.write_text(text, encoding="utf-8")
    temp.replace(config)
    print(f"Registered in {config}. Open /hooks in Codex CLI to review and trust the new definitions.")
