"""State reduction independent of USB, Codex, and the device UI."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import secrets

from .protocol import MAX_AGENTS, Action


@dataclass
class Agent:
    id: str
    session_id: str
    label: str
    state: str = "idle"
    reason: str = "Ready"
    detail: str = ""
    event: str = "-"
    pending: dict = field(default_factory=dict)
    owner_pid: int = 0
    owner_created: float = 0
    owner_kind: str = "unknown"
    tab: str = ""
    updated_at: float = 0
    project: str = ""


class Deck:
    def __init__(self):
        # Insertion order is deliberate: notifications never reorder rows.
        self.agents: dict[str, Agent] = {}
        self.session = secrets.token_hex(4)
        self.revision = 0
        self.last_sequence = 0
        self.overflow = 0
        self.names: dict[str, str] = {}

    def new_connection(self):
        self.session = secrets.token_hex(4)
        self.last_sequence = 0

    @staticmethod
    def _show_pending(agent):
        latest = next(reversed(agent.pending.values()))
        agent.state = "waiting"
        agent.reason = "Approval needed" if latest["kind"] == "PermissionRequest" else "Question"
        if len(agent.pending) > 1:
            agent.reason += f" +{len(agent.pending) - 1}"
        agent.detail = latest.get("detail", "Review on PC")

    def apply(self, index: int, event: dict):
        session_id = event["session_id"]
        agent_id = hashlib.sha256(session_id.encode()).hexdigest()[:16]
        kind = event["kind"]
        if kind == "SessionName":
            if event.get("name"):
                self.names[session_id] = event["name"]
            else:
                self.names.pop(session_id, None)
            return
        if kind == "SessionEnd":
            self.agents.pop(agent_id, None)
            return
        agent = self.agents.get(agent_id)
        if agent is None:
            agent = Agent(agent_id, session_id, event.get("label") or "Codex")
            self.agents[agent_id] = agent
        # Ownership belongs to the latest report, not a previous attachment of
        # a resumed conversation. Legacy events have no owner_kind.
        if "owner_pid" in event:
            agent.owner_pid = event["owner_pid"]
            agent.owner_created = event.get("owner_created", 0)
            agent.owner_kind = event.get("owner_kind", "unknown")
        if event.get("tab"):
            agent.tab = event["tab"]
        agent.label = event.get("label") or agent.label
        agent.project = event.get("project") or agent.project
        agent.updated_at = event.get("observed_at", agent.updated_at)
        if kind in {"PermissionRequest", "Question"}:
            key = "|".join(str(event.get(k, "")) for k in ("kind", "turn_id", "tool", "tool_id", "input_hash"))
            if key not in agent.pending:
                agent.event = hashlib.sha256(f"{session_id}:{index}".encode()).hexdigest()[:8]
            agent.pending[key] = event
            self._show_pending(agent)
            return
        if kind == "PostToolUse" and agent.pending:
            # Another parallel tool finishing must not clear a waiting question.
            matching = [key for key, pending in agent.pending.items()
                        if pending.get("tool", "") == event.get("tool", "")
                        and pending.get("turn_id", "") == event.get("turn_id", "")
                        and pending.get("input_hash", "") == event.get("input_hash", "")
                        and (not pending.get("tool_id") or pending["tool_id"] == event.get("tool_id"))]
            if not matching:
                return
            for key in matching:
                del agent.pending[key]
            if agent.pending:
                self._show_pending(agent)
                return
        if kind in {"SessionStart", "UserPromptSubmit", "PostToolUse", "Stop", "Interrupt", "Error"}:
            agent.pending.clear()
            agent.event = "-"
            agent.state = {"SessionStart": "idle", "Stop": "idle", "Interrupt": "idle", "Error": "error"}.get(kind, "running")
            agent.reason = {"SessionStart": "Ready", "Stop": "Turn stopped", "Interrupt": "Interrupted", "Error": "Error"}.get(kind, "Working")
            agent.detail = event.get("detail", "")

    def view(self, limit=MAX_AGENTS):
        self.overflow = max(0, len(self.agents) - MAX_AGENTS)
        result = []
        labels = [self.names.get(a.session_id, a.label).casefold() for a in self.agents.values()]
        for agent in list(self.agents.values())[:limit]:
            row = asdict(agent)
            row["name"] = self.names.get(agent.session_id, "")
            row["automatic_name"] = agent.label
            row["label"] = row["name"] or agent.label
            if labels.count(row["label"].casefold()) > 1:
                row["label"] = agent.id[:4] + " " + row["label"]
            result.append(row)
        return result

    def wire_view(self):
        self.revision += 1
        return self.session, self.revision, self.view()

    def accept(self, action: Action) -> Agent:
        if action.session != self.session or action.sequence <= self.last_sequence:
            raise ValueError("Stale or duplicate action")
        self.last_sequence = action.sequence
        return self.agents[action.agent_id]


def demo_deck():
    deck = Deck()
    for index, (name, kind, detail) in enumerate([
        ("API service", "PermissionRequest", "Run unit tests?"),
        ("Tests", "UserPromptSubmit", "Writing test cases"),
        ("Refactor", "Stop", "Refactor complete"),
        ("Frontend", "Question", "Which layout?"),
        ("Docs", "UserPromptSubmit", "Updating README"),
    ], 1):
        deck.apply(index, {"session_id": f"demo-{index}", "label": name, "kind": kind, "detail": detail})
    return deck
