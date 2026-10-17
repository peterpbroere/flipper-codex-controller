import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_deck.cli import main
from agent_deck.hook import normalize
from agent_deck.model import Deck
from agent_deck.names import rename_session, resolve_session
from agent_deck.protocol import MAX_LINE, snapshot
from agent_deck.service import drain
from agent_deck.store import EventStore


class NamingTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "events.sqlite3"
        self.store = EventStore(self.path)
        self.addCleanup(self.store.close)
        self.store.append({"kind": "Question", "session_id": "first", "label": "project",
                           "project": "C:/dev/project", "detail": "Which option?"})
        self.deck = Deck()
        self.cursor = drain(self.store, self.deck, 0)
        self.row = self.deck.view()[0]

    def test_name_persists_across_hooks_and_replay_without_changing_alert(self):
        rename_session(self.store, self.deck, self.row["id"][:6], "Frontend")
        self.store.append({"kind": "PostToolUse", "session_id": "first", "label": "project", "tool": "unrelated"})
        self.cursor = drain(self.store, self.deck, self.cursor)
        after = self.deck.view()[0]
        self.assertEqual(("Frontend", "waiting", self.row["event"]),
                         (after["label"], after["state"], after["event"]))
        replay = Deck()
        drain(self.store, replay, 0)
        self.assertEqual(after, replay.view()[0])

    def test_rename_alone_does_not_update_lifecycle_timestamp(self):
        rename_session(self.store, self.deck, "first", "Tests")
        drain(self.store, self.deck, self.cursor)
        after = self.deck.view()[0]
        self.assertEqual(self.row["updated_at"], after["updated_at"])
        self.assertEqual(self.row["pending"], after["pending"])

    def test_reset_restores_project_name_and_keeps_question(self):
        rename_session(self.store, self.deck, "first", "Tests")
        drain(self.store, self.deck, self.cursor)
        rename_session(self.store, self.deck, "first")
        replay = Deck()
        drain(self.store, replay, 0)
        row = replay.view()[0]
        self.assertEqual(("project", "waiting", self.row["event"]), (row["label"], row["state"], row["event"]))

    def test_name_survives_end_and_resume_without_creating_phantom_row(self):
        rename_session(self.store, self.deck, "first", "Tests")
        self.store.append({"kind": "SessionEnd", "session_id": "first"})
        replay = Deck()
        cursor = drain(self.store, replay, 0)
        self.assertEqual([], replay.view())
        self.store.append({"kind": "SessionStart", "session_id": "first", "label": "project"})
        drain(self.store, replay, cursor)
        self.assertEqual("Tests", replay.view()[0]["label"])

    def test_duplicate_names_remain_distinguishable(self):
        self.deck.apply(50, {"kind": "SessionStart", "session_id": "second", "label": "project"})
        self.deck.apply(51, {"kind": "SessionName", "session_id": "first", "name": "Tests"})
        self.deck.apply(52, {"kind": "SessionName", "session_id": "second", "name": "Tests"})
        rows = self.deck.view()
        self.assertNotEqual(rows[0]["label"], rows[1]["label"])
        self.assertTrue(all(row["label"].endswith(" Tests") for row in rows))

    def test_invalid_names_and_selectors_do_not_write_events(self):
        for name in ("", "   ", "x" * 33, "a\nb", "a\tb", "a\x00b", "a\u202eb"):
            with self.subTest(name=repr(name)), self.assertRaises(ValueError):
                rename_session(self.store, self.deck, "first", name)
        for selector in ("bad", "unknown", self.row["id"][:3]):
            with self.subTest(selector=selector), self.assertRaises(ValueError):
                resolve_session(self.deck, selector)
        self.assertEqual([], self.store.read(self.cursor))

    def test_ambiguous_short_id_is_rejected(self):
        self.deck.apply(50, {"kind": "SessionStart", "session_id": "second", "label": "other"})
        for number, agent in enumerate(self.deck.agents.values()):
            agent.id = "abcd" + str(number) * 12
        with self.assertRaises(ValueError):
            resolve_session(self.deck, "abcd")

    def test_pc_list_and_rename_commands(self):
        for arguments in (["rename", self.row["id"], "Frontend"], ["list"], ["rename", "first", "--reset"]):
            output = io.StringIO()
            with patch("sys.argv", ["agent-deck", *arguments, "--store", str(self.path)]), contextlib.redirect_stdout(output):
                main()
            self.assertIn(self.row["id"], output.getvalue())
            if arguments == ["list"]:
                self.assertIn("Frontend", output.getvalue())
                self.assertIn("C:/dev/project", output.getvalue())

    def test_metadata_is_chunked_bounded_and_sanitized(self):
        row = self.row.copy()
        row.update(session_id="s" * 128, project="C:/" + "long path/" * 80 + "\tEND\n", label="\u00c5terst\u00e4llning")
        lines = snapshot("12345678", 1, [row]).splitlines()
        self.assertTrue(all(len(line) + 1 < MAX_LINE for line in lines))
        values = {}
        for line in lines:
            if not line.startswith(b"META\t"):
                continue
            kind, agent, field, offset, total, chunk = line.split(b"\t")
            values.setdefault(field, b"")
            self.assertEqual(int(offset), len(values[field]))
            values[field] += chunk
            self.assertLessEqual(len(values[field]), int(total))
        self.assertEqual(b"s" * 128, values[b"session_id"])
        self.assertEqual(256, len(values[b"project"]))
        self.assertTrue(values[b"project"].endswith(b"..."))

    def test_hook_records_project_path(self):
        row = normalize({"session_id": "test", "hook_event_name": "SessionStart", "cwd": "C:/dev/Frontend"})
        self.assertEqual("C:/dev/Frontend", row["project"])
        self.assertEqual("Frontend", row["label"])


if __name__ == "__main__":
    unittest.main()
