"""Request routing, expiry, and capability tests without running an AI turn."""
import copy
import unittest
from unittest.mock import patch

from agent_deck.controls import (Controls, CONTINUE_TEXT, USER_INPUT_METHOD,
                                 recommended_answers, review_request)

FIRST = "01a14af1-cf93-7563-8aa2-719a56c7e167"
SECOND = "01a14a17-6b50-78e1-a12d-49e8235461bc"


class FakeRPC:
    def __init__(self, callback):
        self.callback = callback
        self.connected = False
        self.calls = []
        self.replies = []
        self.statuses = {FIRST: "active", SECOND: "idle"}

    def connect(self):
        self.connected = True

    def close(self):
        self.connected = False

    def request(self, method, params):
        self.calls.append((method, copy.deepcopy(params)))
        if method == "thread/loaded/list":
            return {"data": list(self.statuses), "nextCursor": None}
        if method in {"thread/resume", "thread/read"}:
            return {"thread": {"status": {"type": self.statuses[params["threadId"]]}}}
        if method == "thread/turns/list":
            return {"data": [{"id": "turn-" + params["threadId"], "status": "inProgress"}]}
        return {}

    def respond(self, request_id, result):
        self.replies.append((request_id, result))
        self.callback({"method": "serverRequest/resolved", "params": {"threadId": FIRST, "requestId": request_id}})


def approval(request="request-1", command="echo hello", **kwargs):
    return {"id": request, "method": "item/commandExecution/requestApproval", "params": {
        "threadId": FIRST, "turnId": "turn-" + FIRST, "itemId": "item-" + request,
        "command": command, "cwd": "C:\\dev", **kwargs}}


def user_input(request="question-1", questions=None):
    return {"id": request, "method": USER_INPUT_METHOD, "params": {
        "threadId": FIRST, "turnId": "turn-" + FIRST, "itemId": "tool-call-1",
        "isBlocking": True, "questions": questions or [{"id": "layout", "header": "Layout",
            "question": "Which layout?", "options": [
                {"label": "Compact", "description": "Use less space."},
                {"label": "Detailed (Recommended)", "description": "Show more information."}]}]}}


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.controls = Controls(FakeRPC)
        self.rows = [{"session_id": FIRST}, {"session_id": SECOND}]
        self.controls.sync(self.rows, force=True)
        self.rpc = self.controls.rpc

    def tearDown(self):
        self.controls.close()

    def row(self, thread=FIRST, device=False):
        return self.controls.decorate([{"session_id": thread}], device=device)[0]

    def test_attach_only_loaded_threads_without_setting_overrides(self):
        resumes = [p for m, p in self.rpc.calls if m == "thread/resume"]
        self.assertEqual(1, len(resumes))  # Idle threads are read, never held open.
        self.assertTrue(all(set(p) == {"threadId", "excludeTurns"} for p in resumes))
        self.assertIn("stop", self.row()["actions"])
        self.assertNotIn("continue", self.row()["actions"])
        self.assertIn("continue", self.row(SECOND)["actions"])

    def test_stop_targets_exact_turn_and_consumes_token(self):
        token = self.row()["control_token"]
        self.controls.execute(FIRST, "stop", token)
        self.assertIn(("turn/interrupt", {"threadId": FIRST, "turnId": "turn-" + FIRST}), self.rpc.calls)
        with self.assertRaises(ValueError):
            self.controls.execute(FIRST, "stop", token)

    def test_continue_sends_only_the_preset_to_selected_thread(self):
        self.controls.execute(SECOND, "continue", self.row(SECOND)["control_token"])
        method, params = self.rpc.calls[-1]
        self.assertEqual("turn/start", method)
        self.assertEqual(SECOND, params["threadId"])
        self.assertEqual([{"type": "text", "text": CONTINUE_TEXT}], params["input"])
        self.assertEqual({"threadId", "input", "clientUserMessageId"}, set(params))

    def test_approval_and_rejection_address_exact_request_and_never_persist_policy(self):
        for verb, decision in (("approve", "accept"), ("reject", "decline")):
            self.controls._message(approval(verb))
            other = self.row(SECOND)["control_token"]
            self.controls.execute(FIRST, verb, self.row()["control_token"])
            self.assertEqual((verb, {"decision": decision}), self.rpc.replies[-1])
            self.assertEqual(other, self.row(SECOND)["control_token"])

    def test_yolo_answers_only_questions_with_one_recommended_choice(self):
        request = user_input()
        self.controls._message(request)
        self.assertEqual([], self.rpc.replies)
        self.controls.set_yolo_enabled(True)
        self.controls._message(request)
        self.assertEqual([(request["id"], {"answers": {"layout": {
            "answers": ["Detailed (Recommended)"]}}})], self.rpc.replies)
        self.controls._message(request)
        self.assertEqual(1, len(self.rpc.replies))

    def test_yolo_leaves_ambiguous_secret_other_and_free_text_questions_unanswered(self):
        cases = [
            [{"id": "one", "header": "A", "question": "Choose", "options": [
                {"label": "A"}, {"label": "B"}]}],
            [{"id": "one", "header": "A", "question": "Choose", "isSecret": True,
              "options": [{"label": "A (Recommended)"}]}],
            [{"id": "one", "header": "A", "question": "Choose", "isOther": True,
              "options": [{"label": "A (Recommended)"}]}],
            [{"id": "one", "header": "A", "question": "Write a response", "options": None}],
            [{"id": "one", "header": "A", "question": "Choose", "options": [
                {"label": "A (Recommended)"}, {"label": "B (Recommended)"}]}],
        ]
        self.controls.set_yolo_enabled(True)
        for index, questions in enumerate(cases):
            self.controls._message(user_input("ambiguous-" + str(index), questions))
        self.assertEqual([], self.rpc.replies)
        self.assertIsNone(recommended_answers({"questions": cases[0]}))

    def test_yolo_auto_approves_command_and_file_requests_only_when_accept_is_available(self):
        self.controls.set_yolo_enabled(True)
        command = approval("yolo-command", availableDecisions=["accept", "decline"])
        file_change = {"id": "yolo-file", "method": "item/fileChange/requestApproval", "params": {
            "threadId": FIRST, "turnId": "turn-" + FIRST, "itemId": "patch-1",
            "availableDecisions": ["accept", "decline"]}}
        directory_grant = {"id": "yolo-grant", "method": "item/fileChange/requestApproval", "params": {
            "threadId": FIRST, "turnId": "turn-" + FIRST, "itemId": "grant-1",
            "grantRoot": "C:/", "availableDecisions": ["accept", "decline"]}}
        unavailable = approval("yolo-unavailable", availableDecisions=["decline"])
        self.controls._message(command)
        self.controls._message(file_change)
        self.controls._message(directory_grant)
        self.controls._message(unavailable)
        self.assertEqual([(command["id"], {"decision": "accept"}),
                          (file_change["id"], {"decision": "accept"})], self.rpc.replies)
        self.assertNotIn("approve", self.row()["actions"])

    def test_parallel_resolution_and_pc_answer_invalidate_stale_screen(self):
        self.controls._message(approval("one"))
        self.controls._message(approval("two"))
        old = self.row()["control_token"]
        self.controls._message({"method": "serverRequest/resolved", "params": {"threadId": FIRST, "requestId": "one"}})
        with self.assertRaises(ValueError):
            self.controls.execute(FIRST, "approve", old)
        self.assertEqual("echo hello", self.controls.contexts[FIRST].requests["two"]["message"]["params"]["command"])

    def test_disconnect_and_reconnect_cannot_replay_approval(self):
        self.controls._message(approval())
        token = self.row()["control_token"]
        self.rpc.connected = False
        self.assertEqual([], self.row()["actions"])
        self.controls.sync(self.rows, force=True)
        with self.assertRaises(ValueError):
            self.controls.execute(FIRST, "approve", token)

    def test_long_preview_keeps_device_approval_for_the_exact_original_request(self):
        for size in (600, 20000):
            message = approval(request="long-" + str(size), command="echo " + "x" * size)
            original = copy.deepcopy(message)
            self.controls._message(message)
            row = self.row(device=True)
            self.assertIn("approve", row["actions"])
            self.assertEqual(512, len(row["review"]))
            self.assertTrue(row["review"].startswith("Preview truncated | Command: | echo "))
            self.assertIn("reject", row["actions"])
            self.controls.execute(FIRST, "approve", row["control_token"], device=True)
            self.assertEqual((message["id"], {"decision": "accept"}), self.rpc.replies[-1])
            self.assertEqual(original, message)

    def test_preview_boundary_does_not_change_approval_capability(self):
        overhead = len(review_request(approval(command="x"), {})["device_review"]) - 1
        for size in (512, 513):
            request = review_request(approval(command="x" * (size - overhead)), {})
            self.assertTrue(request["device_approve"])
            self.assertEqual(512, len(request["device_review"]))
            self.assertEqual(size > 512, request["device_review"].startswith("Preview truncated | "))

    def test_long_file_change_preview_keeps_device_approval(self):
        message = {"method": "item/fileChange/requestApproval", "id": "large-patch",
                   "params": {"itemId": "patch-item"}}
        item = {"changes": [{"path": "example.py", "diff": "x" * 20000}]}
        request = review_request(message, {"patch-item": item})
        self.assertTrue(request["device_approve"])
        self.assertEqual(512, len(request["device_review"]))
        self.assertTrue(request["device_review"].startswith("Preview truncated | File changes:"))

    def test_non_ascii_preview_keeps_approval_and_the_exact_original_request(self):
        for device in (False, True):
            message = approval(command='echo åäö “hello” 🙂', cwd='C:/räksmörgås', reason='Behöver godkännande')
            original = copy.deepcopy(message)
            self.controls._message(message)
            row = self.row(device=device)
            self.assertIn("approve", row["actions"])
            self.assertIn("reject", row["actions"])
            if device:
                self.assertIn("echo aao ?hello? ?", row["review"])
                self.assertIn("C:/raksmorgas", row["review"])
                self.assertTrue(all(32 <= ord(c) <= 126 for c in row["review"]))
            else:
                self.assertIn(message["params"]["command"], row["review"])
            self.controls.execute(FIRST, "approve", row["control_token"], device=device)
            self.assertEqual((message["id"], {"decision": "accept"}), self.rpc.replies[-1])
            self.assertEqual(original, message)

    def test_long_desktop_preview_keeps_approval_for_the_original_request(self):
        message = approval(command="echo " + "å" * 20000)
        original = copy.deepcopy(message)
        self.controls._message(message)
        row = self.row()
        self.assertIn("approve", row["actions"])
        self.assertEqual(16384, len(row["review"]))
        self.assertTrue(row["review"].startswith("Preview truncated\n\nCommand:\necho "))
        self.controls.execute(FIRST, "approve", row["control_token"])
        self.assertEqual((message["id"], {"decision": "accept"}), self.rpc.replies[-1])
        self.assertEqual(original, message)

    def test_desktop_preview_boundary_does_not_change_approval_capability(self):
        overhead = len(review_request(approval(command="x"), {})["review"]) - 1
        for size in (16384, 16385):
            request = review_request(approval(command="x" * (size - overhead)), {})
            self.assertTrue(request["approve"])
            self.assertEqual(16384, len(request["review"]))
            self.assertEqual(size > 16384, request["review"].startswith("Preview truncated\n\n"))

    def test_non_ascii_file_change_preview_keeps_approval_on_both_surfaces(self):
        message = {"method": "item/fileChange/requestApproval", "id": "unicode-patch",
                   "params": {"itemId": "patch-item"}}
        item = {"changes": [{"path": "räksmörgås.py", "diff": "ö" * 20000}]}
        request = review_request(message, {"patch-item": item})
        self.assertTrue(request["approve"])
        self.assertTrue(request["device_approve"])
        self.assertEqual(16384, len(request["review"]))
        self.assertEqual(512, len(request["device_review"]))
        self.assertTrue(request["review"].startswith("Preview truncated\n\n"))
        self.assertTrue(request["device_review"].startswith("Preview truncated | "))
        self.assertIn("raksmorgas.py", request["device_review"])

    def test_no_hidden_scope_and_respect_available_decisions(self):
        review = review_request(approval(availableDecisions=["decline"]), {})
        self.assertFalse(review["approve"])
        self.assertTrue(review["reject"])
        review = review_request(approval(command="å" * 20000, availableDecisions=["decline"]), {})
        self.assertFalse(review["approve"])
        self.assertFalse(review["device_approve"])
        review = review_request(approval(kind="stdin"), {})
        self.assertFalse(review["approve"])
        review = review_request(approval(command=None), {})
        self.assertFalse(review["approve"])

    def test_native_cli_cancel_is_available_as_reject_when_decline_is_absent(self):
        self.controls._message(approval(availableDecisions=["accept", {"acceptWithExecpolicyAmendment": {}}, "cancel"]))
        self.assertIn("approve", self.row(device=True)["actions"])
        self.assertIn("reject", self.row(device=True)["actions"])
        self.controls.execute(FIRST, "reject", self.row()["control_token"])
        self.assertEqual(("request-1", {"decision": "cancel"}), self.rpc.replies[-1])

    def test_turn_completion_preserves_newer_turn(self):
        self.controls._message({"method": "turn/started", "params": {"threadId": FIRST, "turn": {"id": "new"}}})
        token = self.row()["control_token"]
        self.controls._message({"method": "turn/completed", "params": {"threadId": FIRST, "turn": {"id": "old"}}})
        self.assertEqual(token, self.row()["control_token"])

    def test_resync_does_not_change_idle_token_or_change_hook_states(self):
        before = self.row(SECOND)["control_token"]
        self.controls.sync(self.rows, force=True)
        self.assertEqual(before, self.row(SECOND)["control_token"])
        row = {"session_id": SECOND, "state": "waiting", "event": "abcd1234"}
        self.controls.decorate([row])
        self.assertEqual(("waiting", "abcd1234"), (row["state"], row["event"]))

    def test_no_auto_retry_when_decision_unconfirmed(self):
        self.controls._message(approval())
        self.rpc.respond = lambda *args: self.rpc.replies.append(args)
        with patch("agent_deck.controls.time.monotonic", side_effect=[0, 9]):
            with self.assertRaisesRegex(RuntimeError, "not confirmed"):
                self.controls.execute(FIRST, "approve", self.row()["control_token"])
        self.assertEqual(1, len(self.rpc.replies))

    def test_new_approval_hook_refreshes_existing_attachment_once_and_bypasses_poll_delay(self):
        original = self.rpc.request
        def request(method, params):
            result = original(method, params)
            if method == "thread/resume":
                self.rpc.callback(approval())
            return result
        self.rpc.request = request
        self.rpc.calls.clear()
        rows = [{"session_id": FIRST, "state": "waiting", "event": "1234abcd",
                 "pending": {"patch": {"kind": "PermissionRequest"}}}]
        self.controls.next_poll = float("inf")
        self.controls.sync(rows)
        self.assertIn("approve", self.row(device=True)["actions"])
        resumes = [p for m, p in self.rpc.calls if m == "thread/resume"]
        self.assertEqual([{"threadId": FIRST, "excludeTurns": True}], resumes)
        self.controls.sync(rows, force=True)
        self.assertEqual(1, sum(m == "thread/resume" for m, _ in self.rpc.calls))

    def test_replayed_file_approval_recovers_full_item_before_allowing_approval(self):
        message = {"method": "item/fileChange/requestApproval", "id": "patch",
                   "params": {"threadId": FIRST, "turnId": "turn-" + FIRST, "itemId": "change"}}
        self.controls._message(message)
        self.assertNotIn("approve", self.row()["actions"])
        original = self.rpc.request
        def request(method, params):
            if method == "thread/items/read":
                self.assertEqual({"threadId": FIRST, "turnId": "turn-" + FIRST, "itemIds": ["change"]}, params)
                return {"data": [{"turnId": "turn-" + FIRST, "item": {
                    "type": "fileChange", "id": "change", "changes": [
                        {"path": "C:/test.txt", "kind": {"type": "add"}, "diff": "+test"}]}}]}
            return original(method, params)
        self.rpc.request = request
        self.controls.sync(self.rows, force=True)
        self.assertIn("approve", self.row(device=True)["actions"])
        self.assertIn("+test", self.row()["review"])

    def test_closed_subscription_is_refreshed_without_removing_the_agent(self):
        self.controls._message({"method": "thread/closed", "params": {"threadId": FIRST}})
        self.assertNotIn(FIRST, self.controls.subscribed)
        self.controls.sync(self.rows, force=True)
        self.assertIn(FIRST, self.controls.subscribed)
        self.assertIn("stop", self.row()["actions"])

    def test_unloaded_thread_never_resumed_and_idle_history_not_loaded(self):
        self.rpc.statuses.pop(SECOND)
        self.rpc.calls.clear()
        self.controls.sync(self.rows, force=True)
        self.assertNotIn(SECOND, self.controls.contexts)
        self.assertFalse(any(m == "thread/resume" for m, _ in self.rpc.calls))
        self.assertEqual([], self.row(SECOND)["actions"])

    def test_idle_threads_are_unsubscribed_and_independent_owner_is_not_controlled(self):
        self.assertIn(FIRST, self.controls.subscribed)
        self.rpc.statuses[FIRST] = "idle"
        self.controls.sync(self.rows, force=True)
        self.assertNotIn(FIRST, self.controls.subscribed)
        self.assertIn(("thread/unsubscribe", {"threadId": FIRST}), self.rpc.calls)
        self.controls.sync([{"session_id": FIRST, "owner_kind": "session"}], force=True)
        self.assertEqual([], self.row()["actions"])


if __name__ == "__main__":
    unittest.main()
