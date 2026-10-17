"""Manage only the recorded companion bridge, with process identity checks."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import re
import subprocess
import sys
import time

import psutil


ROOT = Path(__file__).resolve().parents[2]


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(value, indent=2), encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return {}


def record_time(record):
    # PowerShell's older records use seven fractional digits.
    timestamp = re.sub(r"(\.\d{6})\d+(?=Z|[+-]|$)", r"\1", record["started"])
    return datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp()


def tail(path, limit=16000):
    try:
        with Path(path).open("rb") as file:
            file.seek(0, 2)
            file.seek(max(0, file.tell() - limit))
            return file.read().decode("utf-8", errors="replace")
    except FileNotFoundError:
        return ""


class BridgeManager:
    def __init__(self, state_dir=None, interpreter=None):
        self.state_dir = Path(state_dir) if state_dir else ROOT / ".agent-deck"
        self.interpreter = Path(interpreter or sys.executable)
        if self.interpreter.name.lower() == "pythonw.exe":
            self.interpreter = self.interpreter.with_name("python.exe")
        self.record_path = self.state_dir / "bridge-process.json"
        self.status_path = self.state_dir / "bridge-status.json"
        self.log_path = self.state_dir / "bridge.stderr.log"
        self._children = {}

    @contextmanager
    def locked(self):
        self.state_dir.mkdir(parents=True, exist_ok=True)
        with (self.state_dir / "bridge-control.lock").open("a+b") as file:
            if file.tell() == 0:
                file.write(b"0")
                file.flush()
            file.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                raise RuntimeError("Another bridge control operation is in progress") from error
            try:
                yield
            finally:
                file.seek(0)
                if os.name == "nt":
                    msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(file.fileno(), fcntl.LOCK_UN)

    def process(self, record=None):
        record = read_json(self.record_path) if record is None else record
        if not record:
            return None
        try:
            process = psutil.Process(int(record["pid"]))
            started = record_time(record)
            if abs(process.create_time() - started) >= 0.01:
                return None  # The old process exited; its PID was reused.
            if os.path.normcase(process.exe()) != os.path.normcase(str(self.interpreter)):
                raise RuntimeError("Recorded bridge executable does not match; no process was changed")
            if os.path.normcase(str(record["executable"])) != os.path.normcase(str(self.interpreter)):
                raise RuntimeError("Recorded bridge interpreter does not match; no process was changed")
            args = process.cmdline()
            if not any(args[i:i + 2] == ["-m", "agent_deck"] for i in range(len(args) - 1)) or not any(
                    arg in {"run", "demo"} for arg in args):
                raise RuntimeError("Recorded process is not an Agent Deck bridge; no process was changed")
            return process if process.is_running() else None
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            return None
        except psutil.AccessDenied as error:
            raise RuntimeError("Could not verify the bridge process; no process was changed") from error
        except (KeyError, TypeError, ValueError) as error:
            raise RuntimeError("Invalid bridge process record; inspect .agent-deck/bridge-process.json") from error

    def health(self):
        record = read_json(self.record_path)
        process = self.process(record)
        if process is None:
            return {"running": False, "connected": False, "message": "Bridge stopped",
                    "error": "The bridge exited. Open activity for details." if record and not record.get("stopped") else ""}
        status = read_json(self.status_path)
        fresh = status.get("instance") == record.get("instance") and bool(record.get("instance")) and (
            0 <= time.time() - status.get("updated_at", 0) < 5)
        if not fresh:
            if 0 <= time.time() - record_time(record) < 5:
                return {"running": True, "connected": False, "message": "Starting bridge…", "error": "", "port": ""}
            return {"running": True, "connected": False, "message": "Connection status unavailable",
                    "error": "Restart the bridge to refresh its connection status.", "port": ""}
        connected = bool(status.get("connected"))
        return {**status, "running": True, "connected": connected,
                "message": "Flipper connected" if connected else "Looking for Flipper"}

    def start(self, port="auto", demo=False, store_path=None):
        with self.locked():
            if self.process() is not None:
                raise RuntimeError("The bridge is already running")
            instance = secrets.token_hex(12)
            args = [str(self.interpreter), "-m", "agent_deck", "demo" if demo else "run", "--port", port,
                    "--status-file", str(self.status_path.resolve()), "--instance", instance]
            if store_path:
                args.extend(["--store", str(Path(store_path).resolve())])
            self.status_path.unlink(missing_ok=True)
            with (self.state_dir / "bridge.stdout.log").open("w", encoding="utf-8") as output, self.log_path.open("w", encoding="utf-8") as errors:
                child = subprocess.Popen(args, cwd=ROOT, stdout=output, stderr=errors,
                                         creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            try:
                created = psutil.Process(child.pid).create_time()
            except psutil.NoSuchProcess as error:
                raise RuntimeError("Bridge could not start. " + tail(self.log_path)[-500:]) from error
            self._children[child.pid] = child
            write_json(self.record_path, {"pid": child.pid,
                       "started": datetime.fromtimestamp(created, timezone.utc).isoformat(),
                       "executable": str(self.interpreter), "instance": instance,
                       "port": port, "demo": demo, "store": str(Path(store_path).resolve()) if store_path else None})
            return child.pid

    def stop(self):
        with self.locked():
            process = self.process()
            if process is None:
                return
            targets = process.children(recursive=True) + [process]
            identities = []
            for target in targets:
                try:
                    identities.append((target, target.create_time()))
                except psutil.NoSuchProcess:
                    pass
            for target, created in identities:
                try:
                    current = psutil.Process(target.pid)
                    if abs(current.create_time() - created) < 0.01:
                        current.terminate()
                except psutil.NoSuchProcess:
                    pass
            _, alive = psutil.wait_procs([target for target, _ in identities], timeout=3)
            for target in alive:
                created = next(stamp for candidate, stamp in identities if candidate.pid == target.pid)
                try:
                    current = psutil.Process(target.pid)
                    if abs(current.create_time() - created) < 0.01:
                        current.kill()
                except psutil.NoSuchProcess:
                    pass
            record = read_json(self.record_path)
            record["stopped"] = True
            write_json(self.record_path, record)
            child = self._children.pop(process.pid, None)
            if child:
                child.wait(timeout=2)

    def restart(self):
        record = read_json(self.record_path)
        self.stop()
        return self.start(record.get("port", "auto"), record.get("demo", False), record.get("store"))


class StatusWriter:
    def __init__(self, path=None, instance=""):
        self.path = Path(path) if path else None
        self.instance = instance
        self.next_write = 0

    def update(self, force=False, **status):
        if self.path is None or (not force and time.monotonic() < self.next_write):
            return
        self.next_write = time.monotonic() + 0.5
        write_json(self.path, {"pid": os.getpid(), "instance": self.instance, "updated_at": time.time(), **status})
