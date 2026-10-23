"""Live controls for already-loaded sessions on Codex's shared local server."""
from dataclasses import dataclass, field
import hashlib
import json
import logging
import re
import secrets
import threading
import time
import uuid

from .codex_rpc import CodexRPC
from .protocol import display_text

log = logging.getLogger(__name__)
ACTION_BITS = {"focus": 1, "stop": 2, "continue": 4, "approve": 8, "reject": 16}
CONTINUE_TEXT = "Continue with the next logical step."
APPROVAL_METHODS = {"item/commandExecution/requestApproval", "item/fileChange/requestApproval"}
USER_INPUT_METHOD = "item/tool/requestUserInput"
RECOMMENDED = re.compile(r"\(\s*recommended\s*\)\s*$", re.IGNORECASE)


def recommended_answers(params):
    """Return exact recommended choice labels only when every question is unambiguous."""
    questions = params.get("questions")
    if not isinstance(questions, list) or not questions:
        return None
    answers = {}
    for question in questions:
        if not isinstance(question, dict) or question.get("isSecret") or question.get("isOther"):
            return None
        question_id = question.get("id")
        options = question.get("options")
        if not isinstance(question_id, str) or not question_id or question_id in answers:
            return None
        if not isinstance(options, list):
            return None
        recommended = [option.get("label") for option in options
                       if isinstance(option, dict) and isinstance(option.get("label"), str)
                       and RECOMMENDED.search(option["label"])]
        if len(recommended) != 1:
            return None
        answers[question_id] = {"answers": [recommended[0]]}
    return {"answers": answers}


@dataclass
class Context:
    status: str = "unknown"
    turn: str = ""
    revision: int = 0
    requests: dict = field(default_factory=dict)
    user_inputs: dict = field(default_factory=dict)
    items: dict = field(default_factory=dict)
    busy: bool = False
    hook_event: str = "-"
    refresh_attachment: bool = False


def review_request(message, items):
    """Keep the exact request while preparing bounded display previews."""
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
    wire = display_text(text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", " | "), 513)
    if len(wire) > 512:
        marker = "Preview truncated | "
        wire = marker + wire[:512 - len(marker)]
    review = text
    if len(review) > 16384:
        marker = "Preview truncated\n\n"
        review = marker + review[:16384 - len(marker)]
    return {"message": message, "review": review, "approve": can_accept,
            "reject": can_reject, "reject_decision": reject_decision, "device_approve": can_accept,
            "device_review": wire if can_accept else (
                "Full request requires PC review. Reject cancels this turn." if reject_decision == "cancel" else
                "Full request requires PC review. Reject is still available.")}


class Controls:
    def __init__(self, rpc_factory=CodexRPC):
        self.rpc_factory = rpc_factory
        self.rpc = None
        self.lock = threading.RLock()
        self.contexts = {}
        self.subscribed = set()
        self.yolo_enabled = False
        self._auto_response_ids = set()
        self.nonce = secrets.token_hex(16)
        self.next_poll = 0
        self.error = ""

    @property
    def connected(self):
        rpc = self.rpc
        return rpc is not None and rpc.connected

    def _changed(self, context):
        context.revision += 1

    def set_yolo_enabled(self, enabled):
        enabled = bool(enabled)
        responses = []
        with self.lock:
            changed = self.yolo_enabled != enabled
            self.yolo_enabled = enabled
            if enabled:
                for context in self.contexts.values():
                    responses.extend(self._prepare_pending_yolo(context))
        if changed:
            log.warning("YOLO state received from Flipper: %s", "enabled" if enabled else "disabled")
        self._send_automatic_responses(responses)

    def _mark_auto_response(self, request_id):
        self._auto_response_ids.add(str(request_id))
        if len(self._auto_response_ids) > 512:
            self._auto_response_ids.pop()

    def _prepare_pending_yolo(self, context):
        responses = []
        for key, request in context.requests.items():
            if (request.get("sent") or key in self._auto_response_ids or
                    context.status != "active" or not context.turn):
                continue
            message = request["message"]
            if not self._can_auto_approve(message):
                continue
            if message["params"].get("turnId") != context.turn:
                continue
            request["sent"] = True
            self._mark_auto_response(key)
            responses.append((message["id"], {"decision": "accept"}, message["method"]))
        for key, message in context.user_inputs.items():
            if key in self._auto_response_ids or context.status != "active":
                continue
            if (message["params"].get("turnId") != context.turn or
                    message["params"].get("isBlocking") is not True):
                continue
            result = recommended_answers(message["params"])
            if result is None:
                continue
            self._mark_auto_response(key)
            responses.append((message["id"], result, message["method"]))
        return responses

    def _send_automatic_responses(self, responses):
        for request_id, result, method in responses:
            try:
                self.rpc.respond(request_id, result)
                if method == USER_INPUT_METHOD:
                    log.warning("YOLO answered recommended choices for request %s", request_id)
                else:
                    log.warning("YOLO auto-approved %s request %s", method, request_id)
            except Exception as error:
                log.warning("YOLO response for %s could not be confirmed; it will not be retried: %s",
                            request_id, error)

    @staticmethod
    def _can_auto_approve(message):
        if message.get("method") not in APPROVAL_METHODS:
            return False
        params = message.get("params", {})
        if message["method"] == "item/commandExecution/requestApproval" and params.get("kind", "command") != "command":
            return False
        if message["method"] == "item/fileChange/requestApproval" and params.get("grantRoot"):
            return False
        decisions = params.get("availableDecisions")
        return decisions is None or "accept" in decisions

    @staticmethod
    def _auto_approval_skip_reason(message, is_current):
        """Explain the eligibility check without logging command or patch contents."""
        if not is_current:
            return "request belongs to a different turn"
        if message.get("method") not in APPROVAL_METHODS:
            return "approval method is unsupported"
        params = message.get("params", {})
        if (message["method"] == "item/commandExecution/requestApproval" and
                params.get("kind", "command") != "command"):
            return "request is not a command approval"
        if message["method"] == "item/fileChange/requestApproval" and params.get("grantRoot"):
            return "request asks for a session-wide write grant"
        decisions = params.get("availableDecisions")
        if decisions is not None and "accept" not in decisions:
            return "accept is not an available decision"
        return "eligible"

    def _message(self, message):
        method, params = message.get("method"), message.get("params", {})
        if "id" in message:
            log.info("Codex server request: %s (fields: %s)", method, ", ".join(params))
        thread = params.get("threadId")
        automatic_response = None
        with self.lock:
            context = self.contexts.get(thread)
            if not context:
                if "id" in message and method in APPROVAL_METHODS | {USER_INPUT_METHOD}:
                    log.warning("YOLO could not handle request %s (%s): session is not tracked",
                                message["id"], method)
                return
            if method in APPROVAL_METHODS and "id" in message:
                key = str(message["id"])
                if key not in context.requests:
                    request = review_request(message, context.items)
                    context.requests[key] = request
                    current_turn = context.turn
                    context.turn = params["turnId"]
                    context.status = "active"
                    is_current = not current_turn or current_turn == params["turnId"]
                    if key in self._auto_response_ids:
                        request["sent"] = True
                    elif self.yolo_enabled:
                        reason = self._auto_approval_skip_reason(message, is_current)
                        log.warning("YOLO saw approval request %s (%s): %s",
                                    key, method, reason)
                        if reason == "eligible":
                            request["sent"] = True
                            self._mark_auto_response(key)
                            automatic_response = (message["id"], {"decision": "accept"}, method)
                    else:
                        log.info("YOLO did not handle approval request %s: device setting is off", key)
                    self._changed(context)
            elif method == USER_INPUT_METHOD and "id" in message:
                key = str(message["id"])
                if key not in context.user_inputs:
                    context.user_inputs[key] = message
                if (key not in self._auto_response_ids and self.yolo_enabled and
                        context.status == "active" and params.get("turnId") == context.turn):
                    response = (recommended_answers(params)
                                if params.get("isBlocking") is True else None)
                    if response is not None:
                        self._mark_auto_response(key)
                        automatic_response = (message["id"], response, method)
            elif method == "serverRequest/resolved":
                request_id = str(params.get("requestId"))
                removed = context.requests.pop(request_id, None)
                removed_input = context.user_inputs.pop(request_id, None)
                if removed or removed_input:
                    self._changed(context)
            elif method == "turn/started":
                turn = params.get("turn", {}).get("id", "")
                if context.turn != turn or context.status != "active":
                    context.turn, context.status = turn, "active"
                    context.requests.clear()
                    context.user_inputs.clear()
                    context.items.clear()
                    context.refresh_attachment = True
                    self._changed(context)
            elif method == "thread/closed":
                context.status, context.turn = "notLoaded", ""
                context.requests.clear()
                context.user_inputs.clear()
                context.refresh_attachment = True
                self.subscribed.discard(thread)
                self._changed(context)
            elif method == "turn/completed":
                # A delayed completion for an old turn must not clear a new one.
                if params.get("turn", {}).get("id") == context.turn:
                    context.status, context.turn = "idle", ""
                    context.requests.clear()
                    context.user_inputs.clear()
                    context.items.clear()
                    self._changed(context)
            elif method == "thread/status/changed":
                status = params.get("status", {}).get("type", "unknown")
                if status != context.status:
                    context.status = status
                    if status != "active":
                        context.turn = ""
                        context.requests.clear()
                        context.user_inputs.clear()
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
        if automatic_response:
            request_id, result, response_method = automatic_response
            try:
                self.rpc.respond(request_id, result)
                if response_method == USER_INPUT_METHOD:
                    log.warning("YOLO answered recommended choices for request %s", request_id)
                else:
                    log.warning("YOLO auto-approved %s request %s", response_method, request_id)
            except Exception as error:
                log.warning("YOLO response for %s could not be confirmed; it will not be retried: %s",
                            request_id, error)

    def _recover_file_reviews(self, thread):
        with self.lock:
            context = self.contexts.get(thread)
            missing = [(key, request["message"]) for key, request in context.requests.items()
                       if request["message"]["method"] == "item/fileChange/requestApproval"
                       and not context.items.get(request["message"]["params"]["itemId"], {}).get("changes")]
        for key, message in missing:
            params = message["params"]
            try:
                result = self.rpc.request("thread/items/read", {"threadId": thread,
                    "turnId": params["turnId"], "itemIds": [params["itemId"]]})
            except RuntimeError as error:
                log.debug("File approval details not available yet: %s", error)
                continue
            with self.lock:
                if key not in context.requests or context.requests[key].get("sent"):
                    continue
                for entry in result.get("data", []):
                    item = entry.get("item", {})
                    if entry.get("turnId") == params["turnId"] and item.get("id") == params["itemId"] and item.get("type") == "fileChange":
                        context.items[item["id"]] = item
                        context.requests[key] = review_request(message, context.items)
                        self._changed(context)

    def sync(self, rows, force=False):
        wanted = set()
        approval_events = {}
        for row in rows:
            if row.get("owner_kind") == "session":
                continue
            try:
                uuid.UUID(row["session_id"])
                wanted.add(row["session_id"])
                if row.get("state") == "waiting" and row.get("event", "-") != "-" and any(
                        p.get("kind") == "PermissionRequest" for p in row.get("pending", {}).values()):
                    approval_events[row["session_id"]] = row["event"]
            except (ValueError, KeyError):
                pass
        if not wanted:
            if self.contexts:
                self.close()
            return
        with self.lock:
            urgent = any(thread in self.contexts and self.contexts[thread].hook_event != token
                         for thread, token in approval_events.items())
        if not force and not urgent and time.monotonic() < self.next_poll:
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
                    context = self.contexts[thread]
                    approval = approval_events.get(thread, "-")
                    refresh = context.refresh_attachment or (approval != "-" and approval != context.hook_event)
                    context.hook_event = approval
                    context.refresh_attachment = False
                params = {"threadId": thread}
                result = self.rpc.request("thread/read", params)
                status = result["thread"].get("status", {}).get("type", "unknown")
                if status == "active" and (thread not in self.subscribed or refresh):
                    if refresh:
                        log.info("Refreshing Codex approval attachment for %s", thread)
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
                self._recover_file_reviews(thread)
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
                if row.get("state") == "waiting" and not row["review"] and any(
                        p.get("kind") == "PermissionRequest" for p in row.get("pending", {}).values()):
                    row["control_hint"] = "Waiting for Codex approval details; use Open on PC if needed."
                    if device:
                        row["detail"] = "Getting approval details. Open on PC if needed."
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
