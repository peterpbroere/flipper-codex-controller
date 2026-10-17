"""Live controls for already-loaded sessions on Codex's shared local server."""
from dataclasses import dataclass, field
import hashlib
import json
import logging
import secrets
import threading
import time
import uuid

from .codex_rpc import CodexRPC

log = logging.getLogger(__name__)
ACTION_BITS = {"focus": 1, "stop": 2, "continue": 4, "approve": 8, "reject": 16}
CONTINUE_TEXT = "Continue with the next logical step."
APPROVAL_METHODS = {"item/commandExecution/requestApproval", "item/fileChange/requestApproval"}


@dataclass
class Context:
    status: str = "unknown"
    turn: str = ""
    revision: int = 0
    requests: dict = field(default_factory=dict)
    items: dict = field(default_factory=dict)
    busy: bool = False


def review_request(message, items):
    """Keep the complete approval scope; never silently approve hidden details."""
    params = message["params"]
    supported = True
    if message["method"] == "item/commandExecution/requestApproval":
        command = params.get("command")
        supported = bool(command) and params.get("kind", "command") == "command"
        text = "Command:\n" + (command or "Not supplied: review on PC")
        text += "\nDirectory:\n" + str(params.get("cwd") or "Not supplied")
        for key in ("additionalPermissions", "networkApprovalContext"):
            if params.get(key):
                text += "\n" + key + ":\n" + json.dumps(params[key], ensure_ascii=False, indent=2)
    else:
        item = items.get(params.get("itemId"), {})
        changes = item.get("changes")
        supported = bool(changes) and not params.get("grantRoot")
        text = "File changes:\n" + (json.dumps(changes, ensure_ascii=False, indent=2) if changes else "Not supplied: review on PC")
        if params.get("grantRoot"):
            text += "\nSession write grant:\n" + params["grantRoot"]
    if params.get("reason"):
        text += "\nReason:\n" + params["reason"]
    decisions = params.get("availableDecisions")
    can_accept = supported and (decisions is None or "accept" in decisions)
    reject_decision = "decline" if decisions is None or "decline" in decisions else (
        "cancel" if "cancel" in decisions else "")
    can_reject = bool(reject_decision)
    if reject_decision == "cancel":
        text += "\nReject cancels this turn."
    # A full preview is required on each surface. Large previews remain available
    # in the CLI, and can still be rejected from Agent Deck.
    can_accept = can_accept and len(text) <= 16384
    wire = text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", " | ")
    device_accept = can_accept and len(wire) <= 512 and all(32 <= ord(c) <= 126 for c in wire)
    return {"message": message, "review": text[:16384], "approve": can_accept,
            "reject": can_reject, "reject_decision": reject_decision, "device_approve": device_accept,
            "device_review": wire if device_accept else (
                "Full request requires PC review. Reject cancels this turn." if reject_decision == "cancel" else
                "Full request requires PC review. Reject is still available.")}


class Controls:
    def __init__(self, rpc_factory=CodexRPC):
        self.rpc_factory = rpc_factory
        self.rpc = None
        self.lock = threading.RLock()
        self.contexts = {}
        self.subscribed = set()
        self.nonce = secrets.token_hex(16)
        self.next_poll = 0
        self.error = ""

    @property
    def connected(self):
        rpc = self.rpc
        return rpc is not None and rpc.connected

    def _changed(self, context):
        context.revision += 1

    def _message(self, message):
        method, params = message.get("method"), message.get("params", {})
        if "id" in message:
            log.info("Codex server request: %s (fields: %s)", method, ", ".join(params))
        thread = params.get("threadId")
        with self.lock:
            context = self.contexts.get(thread)
            if not context:
                return
            if method in APPROVAL_METHODS and "id" in message:
                key = str(message["id"])
                if key not in context.requests:
                    context.requests[key] = review_request(message, context.items)
                    context.turn = params["turnId"]
                    context.status = "active"
                    self._changed(context)
            elif method == "serverRequest/resolved":
                if context.requests.pop(str(params.get("requestId")), None):
                    self._changed(context)
            elif method == "turn/started":
                turn = params.get("turn", {}).get("id", "")
                if context.turn != turn or context.status != "active":
                    context.turn, context.status = turn, "active"
                    context.requests.clear()
                    context.items.clear()
                    self._changed(context)
            elif method == "turn/completed":
                # A delayed completion for an old turn must not clear a new one.
                if params.get("turn", {}).get("id") == context.turn:
                    context.status, context.turn = "idle", ""
                    context.requests.clear()
                    context.items.clear()
                    self._changed(context)
            elif method == "thread/status/changed":
                status = params.get("status", {}).get("type", "unknown")
                if status != context.status:
                    context.status = status
                    if status != "active":
                        context.turn = ""
                        context.requests.clear()
                    self._changed(context)
            elif method in {"item/started", "item/completed"}:
                item = params.get("item", {})
                if item.get("type") == "fileChange" and method == "item/started":
                    context.items[item["id"]] = item
                if method == "item/completed":
                    removed = [key for key, request in context.requests.items()
                               if request["message"]["params"].get("itemId") == item.get("id")]
                    for key in removed:
                        del context.requests[key]
                    if removed:
                        self._changed(context)

    def sync(self, rows, force=False):
        wanted = set()
        for row in rows:
            if row.get("owner_kind") == "session":
                continue
            try:
                uuid.UUID(row["session_id"])
                wanted.add(row["session_id"])
            except (ValueError, KeyError):
                pass
        if not wanted:
            if self.contexts:
                self.close()
            return
        if not force and time.monotonic() < self.next_poll:
            return
        self.next_poll = time.monotonic() + 2
        try:
            if not self.connected:
                self.close()
                self.nonce = secrets.token_hex(16)
                self.rpc = self.rpc_factory(self._message)
                self.rpc.connect()
            loaded = set()
            cursor = None
            while True:
                page = self.rpc.request("thread/loaded/list", {"cursor": cursor})
                loaded.update(page.get("data", []))
                cursor = page.get("nextCursor")
                if not cursor:
                    break
            with self.lock:
                removed = set(self.contexts) - (wanted & loaded)
                for thread in removed:
                    del self.contexts[thread]
            for thread in removed:
                if thread in self.subscribed:
                    self.rpc.request("thread/unsubscribe", {"threadId": thread})
                    self.subscribed.discard(thread)
            for thread in wanted & loaded:
                with self.lock:
                    new = thread not in self.contexts
                    if new:
                        self.contexts[thread] = Context()
                    revision = self.contexts[thread].revision
                params = {"threadId": thread}
                result = self.rpc.request("thread/read", params)
                status = result["thread"].get("status", {}).get("type", "unknown")
                if status == "active" and thread not in self.subscribed:
                    result = self.rpc.request("thread/resume", {"threadId": thread, "excludeTurns": True})
                    self.subscribed.add(thread)
                    status = result["thread"].get("status", {}).get("type", "unknown")
                elif status != "active" and thread in self.subscribed:
                    self.rpc.request("thread/unsubscribe", {"threadId": thread})
                    self.subscribed.discard(thread)
                turn = ""
                if status == "active":
                    turns = self.rpc.request("thread/turns/list", {"threadId": thread, "limit": 1, "itemsView": "notLoaded"})
                    recent = turns.get("data", [])
                    if recent and recent[0].get("status") == "inProgress":
                        turn = recent[0]["id"]
                with self.lock:
                    context = self.contexts.get(thread)
                    # Notifications arriving during the RPC are more current.
                    if context and context.revision == revision and (context.status, context.turn) != (status, turn):
                        context.status, context.turn = status, turn
                        if status != "active":
                            context.requests.clear()
                        self._changed(context)
            self.error = ""
        except Exception as error:
            self.error = str(error) or type(error).__name__
            log.debug("Codex controls unavailable: %s", self.error)
            self.close()
            self.next_poll = time.monotonic() + 5

    def _view(self, thread, device=False):
        context = self.contexts.get(thread)
        if not self.connected or not context:
            return {"actions": [], "control_token": "-", "review": "", "control_hint": "Controls need a session on the shared local Codex server."}
        token = hashlib.sha256(f"{self.nonce}:{thread}:{context.revision}".encode()).hexdigest()[:8]
        actions = []
        request = next(iter(context.requests.values()), None)
        review = ""
        if not context.busy:
            if context.status == "active" and context.turn:
                actions.append("stop")
            if context.status == "idle" and not context.requests:
                actions.append("continue")
            if request:
                review = request["device_review" if device else "review"]
                if not request.get("sent") and request["device_approve" if device else "approve"]:
                    actions.append("approve")
                if not request.get("sent") and request["reject"]:
                    actions.append("reject")
        return {"actions": actions, "control_token": token, "review": review,
                "control_hint": "Review on PC" if request and "approve" not in actions else "Shared local Codex session"}

    def decorate(self, rows, device=False):
        with self.lock:
            for row in rows:
                row.update(self._view(row["session_id"], device))
                if row.get("tab") or (self.connected and row["session_id"] in self.contexts):
                    row["actions"].insert(0, "focus")
        return rows

    def execute(self, thread, verb, token, device=False):
        with self.lock:
            view = self._view(thread, device)
            if token != view["control_token"] or verb not in view["actions"]:
                raise ValueError("Session or request changed. Review it again.")
            context = self.contexts[thread]
            turn = context.turn
            request = next(iter(context.requests.values()), None)
            context.busy = True
            self._changed(context)  # Consume this token before sending any action.
        try:
            if verb == "stop":
                self.rpc.request("turn/interrupt", {"threadId": thread, "turnId": turn})
            elif verb == "continue":
                self.subscribed.add(thread)  # turn/start subscribes this connection.
                result = self.rpc.request("turn/start", {"threadId": thread,
                    "clientUserMessageId": str(uuid.uuid4()),
                    "input": [{"type": "text", "text": CONTINUE_TEXT}]})
                with self.lock:
                    if context.status == "idle":
                        context.status = "active" if result.get("turn", {}).get("id") else "unknown"
                        context.turn = result.get("turn", {}).get("id", "")
            elif verb in {"approve", "reject"}:
                with self.lock:
                    request["sent"] = True
                self.rpc.respond(request["message"]["id"], {"decision": "accept" if verb == "approve" else request["reject_decision"]})
                # Sending is not acknowledgement: wait for the server to resolve
                # this exact request. A disconnect never retries the decision.
                until = time.monotonic() + 8
                while time.monotonic() < until:
                    with self.lock:
                        if str(request["message"]["id"]) not in context.requests:
                            return
                    if not self.connected:
                        break
                    time.sleep(.025)
                raise RuntimeError("Decision sent but not confirmed. Check the session on PC.")
        finally:
            with self.lock:
                context.busy = False
                self._changed(context)
            self.next_poll = 0

    def close(self):
        if self.rpc:
            self.rpc.close()
            self.rpc = None
        with self.lock:
            self.contexts.clear()
            self.subscribed.clear()
