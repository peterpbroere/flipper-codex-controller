"""JSON-RPC over the existing Codex daemon's local WebSocket proxy.

The CLI owns the control socket and its access checks. No TCP listener, new
daemon, login credentials, or terminal key injection is needed here.
"""
from concurrent.futures import Future
import json
import os
import queue
import shutil
import socket
import subprocess
import threading

import websocket


class ProxySocket:
    """Socket interface for Codex's byte-transparent stdio proxy (also on Windows)."""
    def __init__(self, executable=None, socket_path=None):
        command = [executable or shutil.which("codex") or "codex", "app-server", "proxy"]
        if socket_path:
            command += ["--sock", str(socket_path)]
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL,
                                        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        self.timeout = 5
        self.chunks = queue.Queue(maxsize=128)
        self.buffer = bytearray()
        self.closed = threading.Event()
        threading.Thread(target=self._read, daemon=True, name="codex-proxy-pipe").start()

    def _read(self):
        try:
            while not self.closed.is_set():
                chunk = self.process.stdout.read1(65536)
                while not self.closed.is_set():
                    try:
                        self.chunks.put(chunk, timeout=0.2)
                        break
                    except queue.Full:
                        pass
                if not chunk:
                    break
        except (OSError, ValueError):
            pass
        finally:
            if not self.closed.is_set():
                self.closed.set()

    def recv(self, size):
        while not self.buffer:
            if self.closed.is_set() and self.chunks.empty():
                return b""
            try:
                chunk = self.chunks.get(timeout=self.timeout)
            except queue.Empty:
                raise socket.timeout("Codex connection timed out")
            if not chunk:
                return b""
            self.buffer.extend(chunk)
        data = bytes(self.buffer[:size])
        del self.buffer[:size]
        return data

    def send(self, data):
        if self.closed.is_set():
            raise OSError("Codex connection closed")
        self.process.stdin.write(data)
        self.process.stdin.flush()
        return len(data)

    def settimeout(self, timeout):
        self.timeout = timeout

    def gettimeout(self):
        return self.timeout

    def shutdown(self, *_):
        self.close()

    def close(self):
        self.closed.set()
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
        for stream in (self.process.stdin, self.process.stdout):
            stream.close()


class CodexRPC:
    def __init__(self, on_message=None, executable=None, socket_path=None):
        self.on_message = on_message or (lambda message: None)
        self.executable = executable
        self.socket_path = socket_path
        self.pipe = None
        self.ws = None
        self.lock = threading.Lock()
        self.pending = {}
        self.counter = 0
        self.closed = threading.Event()
        self.error = ""

    @property
    def connected(self):
        return self.ws is not None and not self.closed.is_set()

    def connect(self):
        self.pipe = ProxySocket(self.executable, self.socket_path)
        try:
            self.ws = websocket.create_connection("ws://localhost/", socket=self.pipe,
                                                   timeout=5, suppress_origin=True,
                                                   enable_multithread=True)
            self.ws.settimeout(1)
            threading.Thread(target=self._receive, daemon=True, name="codex-rpc-reader").start()
            result = self.request("initialize", {
                "clientInfo": {"name": "agent_deck", "title": "Agent Deck", "version": "0.2.0"},
                "capabilities": {"experimentalApi": True}})
            self.notify("initialized", {})
            return result
        except Exception:
            self.close()
            raise

    def _receive(self):
        try:
            while not self.closed.is_set():
                try:
                    raw = self.ws.recv()
                except websocket.WebSocketTimeoutException:
                    continue
                if not raw:
                    raise OSError("Codex server disconnected")
                message = json.loads(raw)
                if "method" not in message and "id" in message:
                    with self.lock:
                        future = self.pending.pop(message["id"], None)
                    if future and not future.done():
                        if "error" in message:
                            future.set_exception(RuntimeError(message["error"].get("message", "Codex rejected request")))
                        else:
                            future.set_result(message.get("result", {}))
                else:
                    self.on_message(message)
        except Exception as error:
            self.error = str(error)
        finally:
            self.closed.set()
            with self.lock:
                pending, self.pending = self.pending, {}
            for future in pending.values():
                if not future.done():
                    future.set_exception(RuntimeError(self.error or "Codex connection closed"))

    def request(self, method, params, timeout=10):
        if not self.connected:
            raise RuntimeError("Codex controls are disconnected")
        with self.lock:
            self.counter += 1
            request_id = "deck-" + str(self.counter)
            future = Future()
            self.pending[request_id] = future
        try:
            self.ws.send(json.dumps({"id": request_id, "method": method, "params": params}))
            return future.result(timeout=timeout)
        finally:
            with self.lock:
                self.pending.pop(request_id, None)

    def notify(self, method, params):
        self.ws.send(json.dumps({"method": method, "params": params}))

    def respond(self, request_id, result):
        if not self.connected:
            raise RuntimeError("Codex controls are disconnected")
        self.ws.send(json.dumps({"id": request_id, "result": result}))

    def close(self):
        self.closed.set()
        # Closing the proxy wakes the reader without waiting for a remote close.
        if self.pipe:
            self.pipe.close()
