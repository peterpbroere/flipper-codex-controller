"""Bounded, printable ASCII protocol shared with the small Flipper app."""
from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata

MAX_AGENTS = 16
MAX_LINE = 192
IDENTIFIER = re.compile(r"[a-zA-Z0-9_-]{1,16}\Z")
TOKEN = re.compile(r"[a-f0-9]{8}\Z")
STATES = {"idle", "running", "waiting", "done", "error", "offline"}
VERBS = {"focus", "stop", "continue", "approve", "reject"}


def display_text(value: str, limit: int) -> str:
    normalized = unicodedata.normalize("NFKD", str(value))
    text = "".join(c for c in normalized if not unicodedata.combining(c)).encode("ascii", "replace").decode()
    return "".join(c if 32 <= ord(c) <= 126 else " " for c in text)[:limit]


@dataclass(frozen=True)
class Action:
    session: str
    sequence: int
    agent_id: str
    verb: str
    token: str


def parse_action(raw: bytes) -> Action:
    if len(raw) > MAX_LINE or not raw.endswith(b"\n"):
        raise ValueError("Incomplete or oversized action")
    fields = raw.decode("ascii").rstrip("\r\n").split("\t")
    if len(fields) != 6 or fields[0] != "ACT":
        raise ValueError("Expected ACT session sequence agent verb token")
    _, session, sequence, agent_id, verb, token = fields
    if not TOKEN.fullmatch(session) or not IDENTIFIER.fullmatch(agent_id):
        raise ValueError("Invalid session or agent")
    if not sequence.isascii() or not sequence.isdigit() or not 0 < int(sequence) < 2**32:
        raise ValueError("Invalid sequence")
    if verb not in VERBS or (token != "-" and not TOKEN.fullmatch(token)):
        raise ValueError("Invalid action or request token")
    if verb != "focus" and token == "-":
        raise ValueError("Agent actions require a current control token")
    return Action(session, int(sequence), agent_id, verb, token)


def parse_yolo_setting(raw: bytes):
    """Parse a Flipper YOLO state update; None means this is not a valid update."""
    if len(raw) > MAX_LINE or not raw.endswith(b"\n"):
        return None
    try:
        fields = raw.decode("ascii").rstrip("\r\n").split("\t")
    except UnicodeDecodeError:
        return None
    if len(fields) != 3 or fields[:2] != ["SETTING", "YOLO"] or fields[2] not in {"0", "1"}:
        return None
    return fields[2] == "1"


def snapshot(session: str, revision: int, agents: list[dict]) -> bytes:
    if not TOKEN.fullmatch(session) or len(agents) > MAX_AGENTS:
        raise ValueError("Invalid snapshot")
    lines = [f"BEGIN\t{session}\t{revision}\t{len(agents)}"]
    for agent in agents:
        if not IDENTIFIER.fullmatch(agent["id"]) or agent["state"] not in STATES:
            raise ValueError("Invalid agent")
        token = agent.get("event", "-")
        if token != "-" and not TOKEN.fullmatch(token):
            raise ValueError("Invalid event token")
        lines.append("\t".join(("ROW", agent["id"], display_text(agent["label"], 48),
                                agent["state"], token,
                                display_text(agent.get("reason", ""), 30) or "-",
                                display_text(agent.get("detail", ""), 48) or "-",
                                "1" if "focus" in agent.get("actions", ["focus"] if agent.get("tab") else []) else "0")))
        from .controls import ACTION_BITS
        mask = sum(ACTION_BITS[verb] for verb in set(agent.get("actions", ["focus"] if agent.get("tab") else [])))
        control = agent.get("control_token", "-")
        if control != "-" and not TOKEN.fullmatch(control):
            raise ValueError("Invalid control token")
        if mask & 30 and control == "-":
            raise ValueError("Actions need a control token")
        lines.append(f"CTRL\t{agent['id']}\t{mask}\t{control}")
        for field, limit in (("session_id", 128), ("project", 256), ("review", 512)):
            original = agent.get(field, "")
            value = display_text(original, limit)
            if field == "project" and len(display_text(original, limit + 1)) > limit:
                value = value[:-3] + "..."
            for offset in range(0, len(value), 96):
                lines.append("\t".join(("META", agent["id"], field, str(offset), str(len(value)), value[offset:offset + 96])))
    lines.append(f"END\t{revision}")
    return ("\n".join(lines) + "\n").encode("ascii")
