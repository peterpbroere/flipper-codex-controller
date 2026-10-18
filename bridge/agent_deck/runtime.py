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

from .app_paths import SOURCE_ROOT, app_root, bridge_executable, is_frozen, state_dir as default_state_dir


ROOT = SOURCE_ROOT


def connection_settings(transport="usb", port="auto", address=""):
    if transport not in {"usb", "bluetooth"}:
        raise ValueError("Connection must be USB or Bluetooth")
    port = str(port).strip() or "auto"
    address = str(address).strip()
    if len(port) > 256 or any(ord(c) < 32 for c in port):
        raise ValueError("Invalid USB port")
    if address and not (re.fullmatch(r"(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}", address) or
                        re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", address)):
        raise ValueError("Choose a Bluetooth device from the scan results")
    if transport == "bluetooth" and not address:
        raise ValueError("Choose a paired Bluetooth Flipper first")
    return dict(transport=transport, port=port, address=address)


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
        self.state_dir = Path(state_dir) if state_dir else default_state_dir()
        self.interpreter = Path(interpreter or bridge_executable())
        if self.interpreter.name.lower() == "pythonw.exe":
            self.interpreter = self.interpreter.with_name("python.exe")
        self.record_path = self.state_dir / "bridge-process.json"
        self.status_path = self.state_dir / "bridge-status.json"
        self.log_path = self.state_dir / "bridge.stderr.log"
        self.connection_path = self.state_dir / "connection.json"
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
            expected_executable = os.path.normcase(os.path.realpath(self.interpreter))
            if os.path.normcase(os.path.realpath(process.exe())) != expected_executable:
                raise RuntimeError("Recorded bridge executable does not match; no process was changed")
            if os.path.normcase(os.path.realpath(record["executable"])) != expected_executable:
                raise RuntimeError("Recorded bridge interpreter does not match; no process was changed")
            args = process.cmdline()
            module_entry = any(args[i:i + 2] == ["-m", "agent_deck"] for i in range(len(args) - 1))
            if (not module_entry and not is_frozen()) or not any(arg in {"run", "demo"} for arg in args):
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

    def connection(self):
        saved = read_json(self.connection_path)
        return connection_settings(saved.get("transport", "usb"), saved.get("port", "auto"), saved.get("address", ""))

    def configure(self, transport, port="auto", address=""):
        settings = connection_settings(transport, port, address)
        write_json(self.connection_path, settings)
        return settings

    def start(self, port=None, demo=False, store_path=None, transport=None, address=None):
        with self.locked():
            if self.process() is not None:
                raise RuntimeError("The bridge is already running")
            instance = secrets.token_hex(12)
            settings = self.connection()
            settings = connection_settings(transport or settings["transport"],
                port if port is not None else settings["port"],
                address if address is not None else settings["address"])
            args = [str(self.interpreter)]
            if not is_frozen():
                args.extend(["-m", "agent_deck"])
            args.extend(["demo" if demo else "run", "--port", settings["port"],
                         "--status-file", str(self.status_path.resolve()), "--instance", instance])
            args.extend(["--transport", settings["transport"], "--address", settings["address"]])
            if store_path:
                args.extend(["--store", str(Path(store_path).resolve())])
            self.status_path.unlink(missing_ok=True)
            with (self.state_dir / "bridge.stdout.log").open("w", encoding="utf-8") as output, self.log_path.open("w", encoding="utf-8") as errors:
                child = subprocess.Popen(args, cwd=app_root(), stdout=output, stderr=errors,
                                         creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            try:
                created = psutil.Process(child.pid).create_time()
            except psutil.NoSuchProcess as error:
                raise RuntimeError("Bridge could not start. " + tail(self.log_path)[-500:]) from error
            self._children[child.pid] = child
            write_json(self.record_path, {"pid": child.pid,
                       "started": datetime.fromtimestamp(created, timezone.utc).isoformat(),
                       "executable": str(self.interpreter), "instance": instance,
                       **settings, "demo": demo, "store": str(Path(store_path).resolve()) if store_path else None})
            return child.pid

    def stop(self):
        with self.locked():
            process = self.process()
            if process is None:
                return
            record = read_json(self.record_path)
            if record.get("instance"):
                write_json(self.state_dir / "bridge-stop.json", {"instance": record["instance"]})
                # Let BLE release its GATT session before resorting to termination.
                psutil.wait_procs([process], timeout=4)
            try:
                targets = process.children(recursive=True) + [process] if process.is_running() else []
            except psutil.NoSuchProcess:
                targets = []
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
        return self.start(record.get("port", "auto"), record.get("demo", False), record.get("store"),
                          record.get("transport", "usb"), record.get("address", ""))


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
