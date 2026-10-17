from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from agent_deck.companion import Companion
from agent_deck.runtime import BridgeManager, StatusWriter, read_json, write_json
from agent_deck.store import EventReader, EventStore


class CompanionTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "events.sqlite3"
        self.manager = BridgeManager(Path(self.folder.name) / "runtime")

    def test_reading_an_unconfigured_companion_does_not_create_a_database(self):
        reader = EventReader(self.path)
        self.assertEqual([], reader.read())
        self.assertFalse(self.path.exists())
        result = Companion(self.path, self.manager).refresh()
        self.assertEqual([], result["rows"])
        self.assertFalse(result["health"]["running"])
        self.assertFalse(self.path.exists())

    def test_controller_rename_and_reset_preserve_question_and_selection_id(self):
        store = EventStore(self.path)
        store.append({"kind": "Question", "session_id": "test", "label": "project"})
        store.close()
        controller = Companion(self.path, self.manager)
        before = controller.refresh()["rows"][0]
        renamed = controller.rename(before["id"], "Frontend")["rows"][0]
        self.assertEqual("Frontend", renamed["label"])
        self.assertEqual((before["id"], before["event"], "waiting"),
                         (renamed["id"], renamed["event"], renamed["state"]))
        reset = controller.rename(before["id"])["rows"][0]
        self.assertEqual("project", reset["label"])
        self.assertEqual(before["event"], reset["event"])

    def test_health_failure_is_displayable_and_does_not_discard_sessions(self):
        store = EventStore(self.path)
        store.append({"kind": "SessionStart", "session_id": "test", "label": "project"})
        store.close()
        with patch.object(self.manager, "health", side_effect=RuntimeError("Cannot verify process")):
            result = Companion(self.path, self.manager).refresh()
        self.assertEqual(1, len(result["rows"]))
        self.assertIsNone(result["health"]["running"])
        self.assertIn("Cannot verify", result["error"])

    def test_database_error_is_visible_instead_of_an_empty_success(self):
        self.path.write_text("not a SQLite file")
        result = Companion(self.path, self.manager).refresh()
        self.assertIn("Could not read sessions", result["error"])

    def test_log_read_failure_does_not_freeze_connection_status(self):
        with patch("agent_deck.companion.tail", side_effect=PermissionError("log locked")):
            result = Companion(self.path, self.manager).refresh()
        self.assertFalse(result["health"]["connected"])
        self.assertIn("Could not read activity", result["error"])


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.manager = BridgeManager(Path(self.folder.name) / "runtime")
        self.record = {"pid": 1234, "started": datetime.fromtimestamp(1000, timezone.utc).isoformat(),
                       "executable": str(self.manager.interpreter), "instance": "instance-a"}

    def fake_process(self):
        process = Mock()
        process.create_time.return_value = 1000
        process.exe.return_value = str(self.manager.interpreter)
        process.cmdline.return_value = [str(self.manager.interpreter), "-m", "agent_deck", "run"]
        process.is_running.return_value = True
        return process

    def test_reused_pid_is_never_stopped(self):
        process = self.fake_process()
        process.create_time.return_value = 2000
        with patch("agent_deck.runtime.psutil.Process", return_value=process):
            self.assertIsNone(self.manager.process(self.record))
        process.terminate.assert_not_called()

    def test_legacy_powershell_timestamp_can_be_verified(self):
        self.record["started"] = "1970-01-01T00:16:40.0000000Z"
        process = self.fake_process()
        with patch("agent_deck.runtime.psutil.Process", return_value=process):
            self.assertIs(process, self.manager.process(self.record))

    def test_unrelated_executable_and_command_are_refused(self):
        for change in ("executable", "command"):
            process = self.fake_process()
            if change == "executable":
                process.exe.return_value = "unrelated.exe"
            else:
                process.cmdline.return_value = [str(self.manager.interpreter), "personal_script.py"]
            with self.subTest(change=change), patch("agent_deck.runtime.psutil.Process", return_value=process):
                with self.assertRaises(RuntimeError):
                    self.manager.process(self.record)
            process.terminate.assert_not_called()

    @unittest.skipIf(os.name == "nt", "POSIX Python interpreter symlink")
    def test_interpreter_symlink_matches_the_actual_executable(self):
        alias = Path(self.folder.name) / "python-alias"
        alias.symlink_to(sys.executable)
        self.manager = BridgeManager(Path(self.folder.name) / "runtime", interpreter=alias)
        self.record["executable"] = str(alias)
        process = self.fake_process()
        process.exe.return_value = str(Path(sys.executable).resolve())
        with patch("agent_deck.runtime.psutil.Process", return_value=process):
            self.assertIs(process, self.manager.process(self.record))

    def test_only_current_fresh_heartbeat_can_report_connected(self):
        write_json(self.manager.record_path, self.record)
        for token, age, expected in (("instance-a", 0, True), ("instance-a", 20, False), ("old-instance", 0, False)):
            write_json(self.manager.status_path, {"instance": token, "updated_at": time.time() - age,
                                                 "connected": True, "port": "COM4"})
            with self.subTest(token=token, age=age), patch.object(self.manager, "process", return_value=Mock()):
                self.assertEqual(expected, self.manager.health()["connected"])

    def test_stopped_bridge_never_uses_a_stale_connected_file(self):
        write_json(self.manager.status_path, {"instance": "old", "updated_at": time.time(), "connected": True})
        self.assertFalse(self.manager.health()["connected"])
        self.assertFalse(self.manager.health()["running"])

    def test_first_heartbeat_is_allowed_to_arrive_before_showing_an_error(self):
        self.record["started"] = datetime.now(timezone.utc).isoformat()
        write_json(self.manager.record_path, self.record)
        with patch.object(self.manager, "process", return_value=Mock()):
            health = self.manager.health()
        self.assertEqual("Starting bridge…", health["message"])
        self.assertEqual("", health["error"])
        self.assertFalse(health["connected"])

    def test_status_writer_publishes_instance_and_stopped_state(self):
        status = StatusWriter(self.manager.status_path, "test-instance")
        status.update(connected=True, running=True)
        self.assertEqual("test-instance", read_json(self.manager.status_path)["instance"])
        status.update(force=True, connected=False, running=False)
        self.assertFalse(read_json(self.manager.status_path)["connected"])

    def test_bridge_lock_prevents_overlapping_start_stop_operations(self):
        with self.manager.locked():
            with self.assertRaises(RuntimeError):
                with BridgeManager(self.manager.state_dir).locked():
                    self.fail("second control operation acquired the lock")

    def test_real_background_start_status_duplicate_prevention_and_stop(self):
        database = Path(self.folder.name) / "isolated.sqlite3"
        # An invalid serial port keeps this integration test away from real USB.
        try:
            pid = self.manager.start(port="COM987", store_path=database)
            self.assertGreater(pid, 0)
            with self.assertRaises(RuntimeError):
                self.manager.start(port="COM987", store_path=database)
            deadline = time.monotonic() + 6
            while time.monotonic() < deadline:
                health = self.manager.health()
                if health["message"] == "Looking for Flipper":
                    break
                time.sleep(.05)
            self.assertEqual("Looking for Flipper", health["message"])
            self.assertFalse(health["connected"])
            self.assertTrue(database.exists())
        finally:
            self.manager.stop()
        self.assertFalse(self.manager.health()["running"])
        self.assertEqual("", self.manager.health()["error"])


if __name__ == "__main__":
    unittest.main()
