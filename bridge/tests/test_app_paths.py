import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from agent_deck.app_paths import bridge_executable


class AppPathTests(unittest.TestCase):
    def test_frozen_companion_uses_sibling_console_executable(self):
        suffix = ".exe" if os.name == "nt" else ""
        with tempfile.TemporaryDirectory() as folder:
            companion = Path(folder) / f"AgentDeckCompanion{suffix}"
            console = Path(folder) / f"AgentDeck{suffix}"
            companion.touch()
            console.touch()
            with patch.object(sys, "frozen", True, create=True), \
                    patch.object(sys, "executable", str(companion)):
                self.assertEqual(console.resolve(), bridge_executable())


if __name__ == "__main__":
    unittest.main()
