"""Owned-window callback tests; no OS input or desktop automation."""
from pathlib import Path
import gc
import tempfile
import time
import unittest
from unittest.mock import patch

from agent_deck.companion import Companion
from agent_deck.runtime import BridgeManager
from agent_deck.store import EventStore

try:
    import tkinter as tk
    from agent_deck.gui import CompanionWindow
except ImportError:
    tk = None


@unittest.skipIf(tk is None, "Tkinter is not installed")
class WindowTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as error:
            self.skipTest("No graphical display: " + str(error))
        self.addCleanup(self.cleanup_window)
        self.root.withdraw()
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name) / "events.sqlite3"
        store = EventStore(self.path)
        for session in ("first", "second"):
            store.append({"kind": "SessionStart", "session_id": session, "label": session})
        store.close()
        controller = Companion(self.path, BridgeManager(Path(self.folder.name) / "runtime"))
        self.app = CompanionWindow(self.root, controller)
        self.pump()

    def cleanup_window(self):
        if hasattr(self, "app"):
            self.app.close()
            # Finish queued work while the main thread still owns every Tk
            # object. A later test's worker must never collect an old window.
            self.app.worker.shutdown(wait=True)
            self.app = None
        elif hasattr(self, "root"):
            self.root.destroy()
        self.root = None
        gc.collect()
        if hasattr(self, "folder"):
            self.folder.cleanup()

    def pump(self):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            self.root.update()
            if not self.app.busy:
                self.root.update()
                return
            time.sleep(.01)
        self.fail("Window operation did not complete")

    def test_background_refresh_preserves_unsaved_text_and_editor_availability(self):
        self.app.name.set("An unsaved session name")
        self.app._submit(self.app.controller.refresh)
        self.assertNotEqual("disabled", str(self.app.name_entry.cget("state")))
        self.pump()
        self.assertEqual("An unsaved session name", self.app.name.get())

    def test_rename_keeps_the_second_session_selected_and_updates_editor(self):
        second = list(self.app.rows)[1]
        self.app.table.selection_set(second)
        self.root.update()
        self.app.name.set("Frontend")
        self.app._rename()
        self.pump()
        self.assertEqual(second, self.app.selected)
        self.assertEqual("Frontend", self.app.name.get())
        self.assertEqual("Frontend", self.app.rows[second]["label"])
        self.app._rename(reset=True)
        self.pump()
        self.assertEqual(second, self.app.selected)
        self.assertEqual("second", self.app.name.get())

    def test_invalid_name_error_remains_visible_after_refresh(self):
        self.app.name.set("x" * 33)
        self.app._rename()
        self.pump()
        self.assertIn("1-32", self.app.error.get())
        self.app._submit(self.app.controller.refresh)
        self.pump()
        self.assertIn("1-32", self.app.error.get())

    def test_activity_panel_can_be_opened_and_closed(self):
        self.app._toggle_logs()
        self.assertTrue(self.app.logs_visible)
        self.assertEqual("Hide activity", self.app.activity_button.cget("text"))
        self.app._toggle_logs()
        self.assertFalse(self.app.logs_visible)

    def test_connection_editor_preserves_unsaved_device_during_refresh(self):
        self.app._connection_dialog()
        self.app.bluetooth_selection.set("80:E1:27:37:4B:48")
        self.app._submit(self.app.controller.refresh)
        self.pump()
        self.assertEqual("80:E1:27:37:4B:48", self.app.bluetooth_selection.get())
        self.app.connection_dialog.destroy()

    def test_bluetooth_scan_results_identify_device_and_connection_hint(self):
        self.app._connection_dialog()
        data = self.app.controller.refresh()
        data["bluetooth_devices"] = [dict(name="Deck test", address="80:E1:27:37:4B:48")]
        data["health"].update(connected=True, transport="bluetooth", port="80:E1:27:37:4B:48")
        self.app._render(data)
        self.assertIn("Deck test", self.app.bluetooth_selection.get())
        self.assertIn("Bluetooth", self.app.connection_hint.get())
        self.app.connection_dialog.destroy()

    def test_agent_buttons_follow_capabilities_and_confirmation_keeps_original_target(self):
        first, second = list(self.app.rows)
        self.app.table.selection_set(first)
        self.root.update()
        self.app.rows[first].update(actions=["stop"], control_token="1234abcd")
        self.app._buttons()
        self.assertEqual("normal", str(self.app.agent_buttons["stop"].cget("state")))
        self.assertEqual("disabled", str(self.app.agent_buttons["approve"].cget("state")))
        data = self.app.controller.refresh()
        def confirm(*args, **kwargs):
            self.app.table.selection_set(second)
            self.root.update()
            return True
        with patch("agent_deck.gui.messagebox.askyesno", side_effect=confirm), \
             patch.object(self.app.controller, "act", return_value=data) as act:
            self.app._agent_action("stop")
            self.pump()
            act.assert_called_once_with(first, "stop", "1234abcd")


if __name__ == "__main__":
    unittest.main()
