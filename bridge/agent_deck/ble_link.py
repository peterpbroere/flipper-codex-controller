"""Synchronous bridge interface to one asynchronous, authenticated BLE link."""
from __future__ import annotations

import asyncio
import logging
import queue
import threading
import time

from .bluetooth import ProbeLink, backend

log = logging.getLogger(__name__)


class BluetoothLink:
    ack_timeout = 10

    def __init__(self, address):
        if not address:
            raise ValueError("Choose a paired Bluetooth Flipper first")
        self.address = address
        self.client_type, self.scanner = backend()
        self.name = address
        self.last_ack = 0
        self.error = ""
        self.next_attempt = 0
        self.opening = None
        self.closing = None
        self.writes = []
        self.client = None
        self.stream = None
        self.reader = None
        self.ready = False
        self.incoming = queue.Queue(maxsize=256)
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._worker, name="agent-deck-bluetooth", daemon=True)
        self.thread.start()

    def _worker(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()
        self.loop.close()

    @property
    def connected(self):
        return bool(self.ready and not self.error and self.client and self.client.is_connected)

    @property
    def busy(self):
        return any(not future.done() for future in self.writes)

    async def _open(self):
        await self._disconnect()
        device = await self.scanner.find_device_by_address(self.address, timeout=8)
        if device is None:
            raise RuntimeError("Selected Bluetooth Flipper not found; choose BLE in Agent Deck settings")
        self.client = self.client_type(device, pair=True, timeout=30)
        try:
            await self.client.connect()
            self.stream = ProbeLink(self.client, protocol="AD3")
            await self.stream.start()
            self.write_lock = asyncio.Lock()
            self.reader = asyncio.create_task(self._read())
        except BaseException:
            await self._disconnect()
            raise

    async def _read(self):
        try:
            while True:
                try:
                    line = await self.stream.receive(1)
                except asyncio.TimeoutError:
                    continue
                self.incoming.put_nowait(line)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.error = str(error) or type(error).__name__

    def connect(self):
        if self.connected:
            return True
        if self.closing and not self.closing.done():
            return False
        if self.opening:
            if not self.opening.done():
                return False
            opening, self.opening = self.opening, None
            try:
                opening.result()
                self.ready = True
                self.last_ack = time.monotonic()
                log.info("Connected to Agent Deck via Bluetooth on %s", self.address)
                return self.connected
            except Exception as error:
                self.error = (str(error) or type(error).__name__) + ". Pair Deck <name> in Windows Settings if needed."
                log.warning("Bluetooth connection: %s", self.error)
                self.next_attempt = time.monotonic() + 3
        if time.monotonic() >= self.next_attempt:
            self.ready = False
            self.error = ""
            for future in self.writes:
                future.cancel()
            self.writes.clear()
            self._clear_queue()
            self.opening = asyncio.run_coroutine_threadsafe(self._open(), self.loop)
        return False

    async def _send(self, data):
        async with self.write_lock:
            await self.stream.send(data)

    def _check_writes(self):
        pending = []
        for future in self.writes:
            if future.done():
                try:
                    future.result()
                except Exception as error:
                    raise OSError(str(error) or "Bluetooth write failed") from error
            else:
                pending.append(future)
        self.writes = pending

    def send(self, data):
        self._check_writes()
        if not self.connected:
            raise OSError(self.error or "Bluetooth disconnected")
        if len(self.writes) >= 4:
            raise OSError("Bluetooth send queue is full")
        self.writes.append(asyncio.run_coroutine_threadsafe(self._send(bytes(data)), self.loop))

    def receive(self):
        self._check_writes()
        if not self.connected:
            raise OSError(self.error or "Bluetooth disconnected")
        result = []
        while True:
            try:
                result.append(self.incoming.get_nowait())
            except queue.Empty:
                return result

    def _clear_queue(self):
        while True:
            try:
                self.incoming.get_nowait()
            except queue.Empty:
                return

    async def _disconnect(self):
        reader, self.reader = self.reader, None
        client, self.client = self.client, None
        self.stream = None
        if reader:
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
        if client:
            await client.disconnect()

    def close(self):
        self.ready = False
        if self.opening:
            self.opening.cancel()
            self.opening = None
        for future in self.writes:
            future.cancel()
        self.writes.clear()
        self._clear_queue()
        self.closing = asyncio.run_coroutine_threadsafe(self._disconnect(), self.loop)
        self.next_attempt = time.monotonic() + 2

    def shutdown(self):
        self.close()
        try:
            self.closing.result(timeout=5)
        except Exception as error:
            log.debug("Bluetooth shutdown: %s", error)
        finally:
            async def cancel_tasks():
                tasks = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
            asyncio.run_coroutine_threadsafe(cancel_tasks(), self.loop).result(timeout=3)
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.thread.join(timeout=3)
