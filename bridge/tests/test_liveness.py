from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import psutil

from agent_deck.model import Deck
from agent_deck.processes import classify_owner, is_alive, owner_context
from agent_deck.service import drain, remove_dead
from agent_deck.store import EventStore


def process(pid=123, args=None, created=100):
    p = Mock(pid=pid)
    p.name.return_value = "codex.exe"
    p.cmdline.return_value = args or ["codex.exe", "--no-daemon"]
    p.create_time.return_value = created
    p.is_running.return_value = True
    return p


def report(kind="Stop", session="first", owner_kind="shared", age=301):
    return {"session_id": session, "kind": kind, "label": session,
            "owner_pid": 123, "owner_created": 100, "owner_kind": owner_kind,
            "observed_at": time.time() - age}


class ProcessTests(unittest.TestCase):
    def test_dedicated_cli_owns_its_server_child(self):
        server = process(123, ["codex.exe", "app-server", "--listen", "stdio://"])
        cli = process(456)
        with patch("agent_deck.processes.psutil.Process") as factory:
            factory.return_value.parents.return_value = [server, cli]
            context = owner_context()
        self.assertEqual((456, "session"), (context["owner_pid"], context["owner_kind"]))

    def test_shared_server_is_not_a_session_owner(self):
        server = process(args=["codex.exe", "app-server", "--managed-daemon"])
        with patch("agent_deck.processes.psutil.Process") as factory:
            factory.return_value.parents.return_value = [server]
            self.assertEqual("shared", owner_context()["owner_kind"])

    def test_daemon_launcher_is_not_mistaken_for_a_session_owner(self):
        server = process(args=["codex.exe", "app-server", "--managed-daemon"])
        launcher = process(456, ["codex.exe", "resume"])
        with patch("agent_deck.processes.psutil.Process") as factory:
            factory.return_value.parents.return_value = [server, launcher]
            context = owner_context()
        self.assertEqual((123, "shared"), (context["owner_pid"], context["owner_kind"]))

    def test_inaccessible_arguments_are_unknown(self):
        p = process()
        p.cmdline.side_effect = psutil.AccessDenied(123)
        with patch("agent_deck.processes.psutil.Process", return_value=p):
            self.assertEqual("unknown", classify_owner(123, 100))
            self.assertTrue(is_alive(123, 100))

    def test_access_denied_is_not_a_dead_process(self):
        with patch("agent_deck.processes.psutil.Process", side_effect=psutil.AccessDenied(123)):
            self.assertIsNone(is_alive(123, 100))
            self.assertEqual("unknown", classify_owner(123, 100))

    def test_reused_pid_cannot_verify_or_classify_owner(self):
        with patch("agent_deck.processes.psutil.Process", return_value=process(created=200)):
            self.assertFalse(is_alive(123, 100))
            self.assertEqual("unknown", classify_owner(123, 100))

    def test_old_journal_owner_can_be_classified(self):
        for args, expected in [(["codex.exe", "resume", "--no-daemon"], "session"),
                               (["codex.exe", "app-server"], "shared")]:
            with self.subTest(expected=expected), patch(
                    "agent_deck.processes.psutil.Process", return_value=process(args=args)):
                self.assertEqual(expected, classify_owner(123, 100))


class LivenessTests(unittest.TestCase):
    def setUp(self):
        self.deck = Deck()

    def cleanup(self, alive=True, legacy_kind="unknown"):
        with patch("agent_deck.service.is_alive", return_value=alive), patch(
                "agent_deck.service.classify_owner", return_value=legacy_kind):
            remove_dead(self.deck)

    def test_live_shared_owner_preserves_all_reported_states_without_activity(self):
        for i, kind in enumerate(("Stop", "UserPromptSubmit", "Question", "PermissionRequest")):
            self.deck.apply(i, report(kind, str(i), age=90000))
        tokens = [r["event"] for r in self.deck.view()]
        self.cleanup()
        rows = self.deck.view()
        self.assertEqual(["idle", "running", "waiting", "waiting"], [r["state"] for r in rows])
        self.assertEqual("Turn stopped", rows[0]["reason"])
        self.assertEqual(tokens, [r["event"] for r in rows])
        self.assertEqual("idle", next(iter(self.deck.agents.values())).state)

    def test_live_independent_cli_remains_idle_without_activity(self):
        self.deck.apply(1, report(owner_kind="session", age=90000))
        self.cleanup()
        self.assertEqual("idle", self.deck.view()[0]["state"])

    def test_access_denied_does_not_change_the_reported_state(self):
        self.deck.apply(1, report(owner_kind="session"))
        self.cleanup(alive=None)
        self.assertEqual("idle", self.deck.view()[0]["state"])

    def test_new_activity_updates_the_reported_state(self):
        self.deck.apply(1, report())
        self.cleanup()
        agent_id = self.deck.view()[0]["id"]
        self.deck.apply(2, report("UserPromptSubmit", age=0))
        self.assertEqual((agent_id, "running"), (self.deck.view()[0]["id"], self.deck.view()[0]["state"]))
        self.cleanup()
        self.assertEqual("running", self.deck.view()[0]["state"])

    def test_recovery_of_process_access_restores_recorded_idle_state(self):
        self.deck.apply(1, report(owner_kind="session"))
        self.cleanup(alive=None)
        self.cleanup(alive=True)
        self.assertEqual("idle", self.deck.view()[0]["state"])
        self.assertEqual("Turn stopped", self.deck.view()[0]["reason"])

    def test_day_old_sessions_are_not_expired_without_exit_evidence(self):
        for i, kind in enumerate(("Stop", "UserPromptSubmit", "Question")):
            self.deck.apply(i, report(kind, str(i), age=90000))
        token = self.deck.view()[2]["event"]
        self.cleanup()
        self.assertEqual(["idle", "running", "waiting"], [r["state"] for r in self.deck.view()])
        self.assertEqual(token, self.deck.view()[2]["event"])

    def test_legacy_entry_and_bridge_replay_preserve_idle_shared_sessions(self):
        entry = report()
        del entry["owner_kind"]
        # EventStore supplies observed_at from the journal timestamp.
        with tempfile.TemporaryDirectory() as directory:
            store = EventStore(Path(directory) / "events.sqlite3")
            with patch("agent_deck.store.time.time", return_value=entry["observed_at"]):
                store.append(entry)
            for _ in range(2):
                self.deck = Deck()
                drain(store, self.deck, 0)
                self.cleanup(legacy_kind="shared")
                self.assertEqual("shared", self.deck.view()[0]["owner_kind"])
                self.assertEqual("idle", self.deck.view()[0]["state"])
            store.close()

    def test_legacy_independent_owner_remains_verified(self):
        entry = report(age=90000)
        del entry["owner_kind"]
        self.deck.apply(1, entry)
        self.cleanup(legacy_kind="session")
        self.assertEqual("idle", self.deck.view()[0]["state"])
        self.assertEqual("session", self.deck.view()[0]["owner_kind"])

    def test_resume_replaces_old_owner_including_missing_metadata(self):
        self.deck.apply(1, report(owner_kind="shared"))
        self.deck.apply(2, {**report("SessionStart", owner_kind="session", age=0), "owner_pid": 456})
        self.assertEqual((456, "session"), (self.deck.view()[0]["owner_pid"], self.deck.view()[0]["owner_kind"]))
        self.deck.apply(3, {"session_id": "first", "kind": "Stop", "owner_pid": 0})
        self.assertEqual((0, "unknown"), (self.deck.view()[0]["owner_pid"], self.deck.view()[0]["owner_kind"]))

    def test_shared_server_exit_removes_all_its_sessions(self):
        self.deck.apply(1, report(session="first"))
        self.deck.apply(2, report(session="second"))
        self.cleanup(alive=False)
        self.assertEqual([], self.deck.view())

    def test_live_process_exit_removes_only_its_session_preserving_pending_token(self):
        # Use real OS lifetimes without starting another agent or invoking a model.
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        try:
            created = psutil.Process(child.pid).create_time()
            self.deck.apply(1, {**report(owner_kind="session"), "owner_pid": child.pid, "owner_created": created})
            self.deck.apply(2, {**report("Question", "second", owner_kind="session", age=0),
                                "owner_pid": psutil.Process().pid, "owner_created": psutil.Process().create_time()})
            token = self.deck.view()[1]["event"]
            remove_dead(self.deck)
            self.assertEqual(2, len(self.deck.view()))
            child.terminate()
            child.wait(timeout=5)
            remove_dead(self.deck)
            self.assertEqual(["second"], [r["session_id"] for r in self.deck.view()])
            self.assertEqual(("waiting", token), (self.deck.view()[0]["state"], self.deck.view()[0]["event"]))
        finally:
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
