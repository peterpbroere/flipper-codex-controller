import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from agent_deck.hook import normalize
from agent_deck.install import merged_hooks
from agent_deck.model import Deck, demo_deck
from agent_deck.protocol import MAX_AGENTS, MAX_LINE, Action, parse_action, parse_yolo_setting, snapshot
from agent_deck.service import drain, remove_dead
from agent_deck.store import EventStore
from agent_deck.transport import Lines


def event(kind, session="test-one", tool="Bash", inputs=None, **kwargs):
    return normalize({"session_id": session, "hook_event_name": kind, "cwd": r"C:\dev\API service",
                      "turn_id": "turn-one", "tool_name": tool, "tool_input": inputs or {"command": "run tests"}, **kwargs})


class ModelTests(unittest.TestCase):
    def setUp(self):
        self.deck = Deck()

    def test_duplicate_request_keeps_notification_token(self):
        self.deck.apply(1, event("PermissionRequest"))
        first = self.deck.view()[0]["event"]
        self.deck.apply(2, event("PermissionRequest"))
        self.assertEqual(first, self.deck.view()[0]["event"])

    def test_same_request_after_resolution_is_a_new_notification(self):
        self.deck.apply(1, event("PermissionRequest"))
        first = self.deck.view()[0]["event"]
        self.deck.apply(2, event("PostToolUse"))
        self.deck.apply(3, event("PermissionRequest"))
        self.assertNotEqual(first, self.deck.view()[0]["event"])

    def test_parallel_tool_does_not_clear_question(self):
        question = {"questions": [{"question": "Which layout?"}]}
        self.deck.apply(1, event("PreToolUse", tool="request_user_input", inputs=question))
        self.deck.apply(2, event("PostToolUse"))
        self.assertEqual("waiting", self.deck.view()[0]["state"])
        self.deck.apply(3, event("PostToolUse", tool="request_user_input", inputs=question))
        self.assertEqual("running", self.deck.view()[0]["state"])

    def test_unrelated_command_does_not_clear_approval(self):
        self.deck.apply(1, event("PermissionRequest"))
        self.deck.apply(2, event("PostToolUse", inputs={"command": "another command"}))
        self.assertEqual("waiting", self.deck.view()[0]["state"])

    def test_parallel_requests_clear_independently_without_realerting(self):
        self.deck.apply(1, event("PermissionRequest", inputs={"command": "first"}))
        self.deck.apply(2, event("PermissionRequest", inputs={"command": "second"}))
        token = self.deck.view()[0]["event"]
        self.assertIn("+1", self.deck.view()[0]["reason"])
        self.deck.apply(3, event("PostToolUse", inputs={"command": "second"}))
        self.assertEqual("waiting", self.deck.view()[0]["state"])
        self.assertEqual(token, self.deck.view()[0]["event"])
        self.deck.apply(4, event("PostToolUse", inputs={"command": "first"}))
        self.assertEqual("running", self.deck.view()[0]["state"])

    def test_approval_description_is_not_part_of_command_identity(self):
        self.deck.apply(1, event("PermissionRequest", inputs={"command": "test", "description": "May I test?"}))
        self.deck.apply(2, event("PostToolUse", inputs={"command": "test", "timeout_ms": 1000}))
        self.assertEqual("running", self.deck.view()[0]["state"])

    def test_stop_interrupt_and_new_prompt_clear_pending(self):
        for kind in ("Stop", "Interrupt", "UserPromptSubmit"):
            with self.subTest(kind=kind):
                self.deck.apply(1, event("PermissionRequest"))
                self.deck.apply(2, event(kind))
                self.assertEqual("-", self.deck.view()[0]["event"])
                self.assertNotEqual("waiting", self.deck.view()[0]["state"])

    def test_session_end_removes_agent(self):
        self.deck.apply(1, event("SessionStart"))
        self.deck.apply(2, event("SessionEnd"))
        self.assertEqual([], self.deck.view())

    def test_alert_does_not_reorder_agents(self):
        self.deck.apply(1, event("SessionStart", "first"))
        self.deck.apply(2, event("SessionStart", "second"))
        before = [row["id"] for row in self.deck.view()]
        self.deck.apply(3, event("PermissionRequest", "second"))
        self.assertEqual(before, [row["id"] for row in self.deck.view()])

    def test_sessions_sharing_a_project_get_distinct_labels(self):
        self.deck.apply(1, event("SessionStart", "first"))
        self.deck.apply(2, event("SessionStart", "second"))
        rows = self.deck.view()
        self.assertNotEqual(rows[0]["label"], rows[1]["label"])
        self.assertTrue(all(row["label"].endswith("API service") for row in rows))

    def test_overflow_is_explicit_and_new_slot_appears_after_close(self):
        for index in range(MAX_AGENTS + 1):
            self.deck.apply(index, event("SessionStart", f"agent-{index}"))
        self.assertEqual(MAX_AGENTS, len(self.deck.view()))
        self.assertEqual(1, self.deck.overflow)
        self.deck.apply(100, event("SessionEnd", "agent-0"))
        self.assertEqual(MAX_AGENTS, len(self.deck.view()))
        self.assertEqual(0, self.deck.overflow)

    def test_replay_generates_identical_tokens_and_order(self):
        events = [event("SessionStart"), event("PermissionRequest"), event("SessionStart", "another")]
        for index, item in enumerate(events, 1):
            self.deck.apply(index, item)
        replay = Deck()
        for index, item in enumerate(events, 1):
            replay.apply(index, item)
        self.assertEqual(self.deck.view(), replay.view())

    def test_focus_replay_and_old_connection_rejected(self):
        self.deck.apply(1, event("SessionStart"))
        agent_id = self.deck.view()[0]["id"]
        action = Action(self.deck.session, 1, agent_id, "focus", "-")
        self.deck.accept(action)
        with self.assertRaises(ValueError): self.deck.accept(action)
        self.deck.new_connection()
        with self.assertRaises(ValueError): self.deck.accept(Action(action.session, 2, agent_id, "focus", "-"))

    def test_dead_owner_removed_and_unknown_owner_keeps_reported_state(self):
        self.deck.apply(1, {**event("Stop"), "observed_at": time.time() - 301})
        with patch("agent_deck.service.is_alive", return_value=None): remove_dead(self.deck)
        self.assertEqual("idle", self.deck.view()[0]["state"])
        with patch("agent_deck.service.is_alive", return_value=False): remove_dead(self.deck)
        self.assertEqual([], self.deck.view())


class ProtocolTests(unittest.TestCase):
    def test_swedish_names_remain_readable(self):
        from agent_deck.protocol import display_text
        self.assertEqual("Aterstallning", display_text("Återställning", 32))

    def test_display_fields_cannot_inject_frames(self):
        rows = demo_deck().view()
        rows[0]["label"] = "API\tEND\n999\x00"
        rows[0]["detail"] = "Swedish åäö\n" * 100
        data = snapshot("abcdef12", 1, rows)
        row_lines = [line for line in data.splitlines() if line.startswith(b"ROW\t")]
        self.assertEqual(len(rows), len(row_lines))
        self.assertTrue(all(len(line) < MAX_LINE for line in data.splitlines()))
        self.assertTrue(all(len(line.split(b"\t")) == 8 for line in row_lines))
        self.assertTrue(all(line.startswith((b"BEGIN\t", b"ROW\t", b"CTRL\t", b"META\t", b"END\t")) for line in data.splitlines()))

    def test_agent_controls_require_a_token_and_actions_are_bounded(self):
        self.assertEqual("focus", parse_action(b"ACT\tabcdef12\t1\tagent1\tfocus\t-\n").verb)
        for verb in ("stop", "continue", "approve", "reject"):
            self.assertEqual(verb, parse_action(f"ACT\tabcdef12\t1\tagent1\t{verb}\t123456ab\n".encode()).verb)
        for raw in (b"ACT\tabcdef12\t1\tagent1\tapprove\t-\n", b"ACT\tabcdef12\t-1\tagent1\tfocus\t-\n",
                    b"ACT\tabcdef12\t4294967296\tagent1\tfocus\t-\n", b"ACT\tabcdef12\t1\tagent1\tfocus\t-",
                    b"ACT\twrong\t1\tagent1\tfocus\t-\n"):
            with self.subTest(raw=raw), self.assertRaises(ValueError): parse_action(raw)

    def test_yolo_setting_parser_accepts_only_well_formed_boolean_updates(self):
        self.assertIs(parse_yolo_setting(b"SETTING\tYOLO\t1\n"), True)
        self.assertIs(parse_yolo_setting(b"SETTING\tYOLO\t0\n"), False)
        for raw in (b"SETTING\tYOLO\t2\n", b"SETTING\tOTHER\t1\n",
                    b"SETTING\tYOLO\t1", b"SETTING\tYOLO\t1\tEXTRA\n",
                    b"SETTING\tYOLO\t\xff\n", b"x" * MAX_LINE + b"\n"):
            with self.subTest(raw=raw):
                self.assertIsNone(parse_yolo_setting(raw))

    def test_chunked_lines_and_oversize_resynchronization(self):
        lines = Lines()
        self.assertEqual([], lines.feed(b"HEL"))
        self.assertEqual([b"HELLO\n", b"ACK\n"], lines.feed(b"LO\nACK\n"))
        self.assertEqual([b"OK\n"], lines.feed(b"x" * 500 + b"ACT\nOK\n"))
        self.assertEqual([], lines.feed(b"ABC\0BAD\n"))


class HookTests(unittest.TestCase):
    def test_normal_session_does_not_need_launcher_environment(self):
        item = event("SessionStart")
        self.assertEqual("API service", item["label"])
        self.assertEqual("test-one", item["session_id"])

    def test_ordinary_pre_tool_is_ignored(self):
        self.assertIsNone(event("PreToolUse"))

    def test_question_summary(self):
        item = event("PreToolUse", tool="functions.request_user_input", inputs={"questions": [{"question": "Which layout?"}]})
        self.assertEqual("Question", item["kind"])
        self.assertEqual("Which layout?", item["detail"])

    def test_hook_merge_preserves_other_integrations_and_is_idempotent(self):
        original = {"description": "My hooks", "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "other tool"}]}]}}
        untouched = copy.deepcopy(original)
        merged = merged_hooks(original, "python agent-deck-hook")
        self.assertEqual(untouched, original)
        self.assertEqual(2, len(merged["hooks"]["Stop"]))
        self.assertEqual(merged, merged_hooks(merged, "python agent-deck-hook"))

    def test_invalid_session_is_rejected(self):
        with self.assertRaises(ValueError): event("SessionStart", "../../bad")


class StoreTests(unittest.TestCase):
    def test_hooks_during_bridge_downtime_recover_without_duplicates(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.sqlite3"
            store = EventStore(path)
            store.append(event("SessionStart"))
            store.append(event("PermissionRequest"))
            deck = Deck()
            cursor = drain(store, deck, 0)
            before = deck.view()
            self.assertEqual(cursor, drain(store, deck, cursor))
            self.assertEqual(before, deck.view())
            store.close()
            store = EventStore(path)
            replay = Deck()
            drain(store, replay, 0)
            self.assertEqual(before, replay.view())
            store.close()

    def test_concurrent_hook_writers_do_not_lose_events(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.sqlite3"
            initial = EventStore(path)
            initial.close()
            errors = []
            def write(index):
                try:
                    store = EventStore(path)
                    for n in range(10): store.append(event("SessionStart", f"agent-{index}-{n}"))
                    store.close()
                except Exception as error: errors.append(error)
            threads = [threading.Thread(target=write, args=(i,)) for i in range(4)]
            for thread in threads: thread.start()
            for thread in threads: thread.join()
            store = EventStore(path)
            self.assertEqual([], errors)
            self.assertEqual(40, len(store.read()))
            store.close()

    def test_hook_process_writes_event_and_returns_no_approval(self):
        import os
        with tempfile.TemporaryDirectory() as directory:
            env = dict(os.environ, AGENT_DECK_STATE_DIR=directory)
            completed = subprocess.run([sys.executable, "-m", "agent_deck.hook"],
                                       input=json.dumps({"session_id": "cli-no-launcher", "hook_event_name": "PermissionRequest", "cwd": "C:/dev/project", "tool_input": {"command": "test"}}),
                                       text=True, capture_output=True, env=env, timeout=3)
            self.assertEqual(0, completed.returncode)
            self.assertEqual({}, json.loads(completed.stdout))
            store = EventStore(Path(directory) / "events.sqlite3")
            self.assertEqual("Approval needed", self._reduce(store)[0]["reason"])
            store.close()

    @staticmethod
    def _reduce(store):
        deck = Deck()
        drain(store, deck, 0)
        return deck.view()


if __name__ == "__main__":
    unittest.main()
