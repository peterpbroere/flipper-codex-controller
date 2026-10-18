"""Optional, non-destructive terminal navigation; never types approval keys."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import re
import subprocess
import shutil
import uuid

from .app_paths import SOURCE_ROOT, is_frozen, resource_root


class FocusRunner:
    def __init__(self, script=None):
        default = (resource_root() if is_frozen() else SOURCE_ROOT) / "scripts/Focus-Agent.ps1"
        self.script = Path(script) if script else default
        self.enabled = os.name == "nt" and self.script.is_file()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="agent-deck-focus")
        self.pending = None

    def submit(self, tab):
        if not self.enabled or not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", tab):
            raise ValueError("No supported terminal mapping")
        if self.pending and not self.pending.done():
            raise ValueError("A terminal focus request is already running")
        self.pending = self.executor.submit(self._run, tab)
        return self.pending

    def open_session(self, session_id, tab="", project=""):
        if tab:
            return self.submit(tab)
        if not self.enabled:
            raise ValueError("Opening a CLI window requires Windows")
        session_id = str(uuid.UUID(session_id))
        executable = shutil.which("codex")
        if not executable:
            raise ValueError("Codex CLI is not installed or is not on PATH")
        if self.pending and not self.pending.done():
            raise ValueError("A terminal request is already running")
        def open_window():
            folder = Path(project) if project else Path.cwd()
            if not folder.is_dir():
                folder = Path.cwd()
            # A second view attaches to the same shared thread; it does not fork
            # or send a prompt. All arguments are passed without a shell.
            subprocess.Popen([executable, "resume", session_id], cwd=str(folder),
                             creationflags=subprocess.CREATE_NEW_CONSOLE)
        self.pending = self.executor.submit(open_window)
        return self.pending

    def _run(self, tab):
        powershell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        result = subprocess.run([str(powershell), "-NoProfile", "-NonInteractive", "-File",
                                 str(self.script), "-Tab", tab], capture_output=True, text=True,
                                errors="replace", timeout=15,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode:
            raise RuntimeError((result.stderr.strip() or result.stdout.strip() or "Terminal not found")[-500:])

    def close(self):
        self.executor.shutdown(wait=True)
