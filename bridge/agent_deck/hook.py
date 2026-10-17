"""Observational Codex hook. Never returns an approval or changes agent input."""
from __future__ import annotations

import hashlib
import json
import ntpath
import re
import sys

from .processes import owner_context
from .store import EventStore

EVENTS = {"SessionStart", "SessionEnd", "UserPromptSubmit", "PermissionRequest", "PreToolUse", "PostToolUse", "Stop", "Interrupt"}


def normalize(payload):
    session = str(payload.get("session_id", ""))
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", session):
        raise ValueError("Invalid Codex session id")
    kind = payload.get("hook_event_name")
    if kind not in EVENTS:
        return None
    tool = str(payload.get("tool_name", ""))
    if kind == "PreToolUse":
        if "request_user_input" not in tool.lower():
            return None
        kind = "Question"
    inputs = payload.get("tool_input") or {}
    # Approval events add metadata such as `description` that is absent from
    # PostToolUse. Match the actual action, not that presentation metadata.
    identity = inputs.get("command", inputs) if isinstance(inputs, dict) else inputs
    raw_input = json.dumps(identity, sort_keys=True, ensure_ascii=True)
    detail = ""
    if isinstance(inputs, dict) and kind in {"PermissionRequest", "Question"}:
        detail = inputs.get("description") or inputs.get("command") or ""
        if kind == "Question":
            questions = inputs.get("questions", [])
            if questions and isinstance(questions[0], dict):
                detail = questions[0].get("question") or questions[0].get("title") or "Review on PC"
    cwd = str(payload.get("cwd", ""))
    label = ntpath.basename(cwd.rstrip("\\/")) or "Codex"
    # Store only the summary needed by the dashboard, not transcripts or prompts.
    return {"session_id": session, "kind": kind, "label": label[:80], "project": cwd[:4096],
            "turn_id": str(payload.get("turn_id", ""))[:128],
            "tool_id": str(payload.get("tool_use_id", ""))[:128],
            "tool": tool[:200], "input_hash": hashlib.sha256(raw_input.encode()).hexdigest()[:16],
            "detail": str(detail)[:200]}


def main():
    try:
        data = sys.stdin.buffer.read(1024 * 1024 + 1)
        if len(data) > 1024 * 1024:
            raise ValueError("Hook payload exceeds 1 MiB")
        event = normalize(json.loads(data))
        if event:
            event.update(owner_context())
            store = EventStore()
            try:
                store.append(event)
            finally:
                store.close()
    except Exception as error:
        # Notifications must never block a Codex operation on failure.
        print(f"Agent Deck notification unavailable: {error}", file=sys.stderr)
    print("{}")


if __name__ == "__main__":
    main()
