"""Optional BLE feasibility probe. No RPC client or agent actions are used.

The experimental FAP owns an authenticated serial GATT service under a separate
profile/MAC/bond store. It is deliberately not the stock Flipper RPC endpoint.
"""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time

from .model import Deck, demo_deck
from .protocol import snapshot
from .service import drain, remove_dead
from .store import EventStore
from .transport import Lines

log = logging.getLogger(__name__)

# ST's UUID arrays in the official 1.5.1 serial service are little-endian.
SERVICE = "8fe5b3d5-2e7f-4a98-2a48-7acc60fe0000"
TX = "19ed82ae-ed21-4c9d-4145-228e61fe0000"
RX = "19ed82ae-ed21-4c9d-4145-228e62fe0000"
FLOW = "19ed82ae-ed21-4c9d-4145-228e63fe0000"
MAX_CREDIT = 486


def backend():
    try:
        from bleak import BleakClient, BleakScanner
    except ImportError as error:
        raise RuntimeError('Install Bluetooth support: python -m pip install -e ".[bluetooth]"') from error
    return BleakClient, BleakScanner


async def scan(timeout=8):
    _, scanner = backend()
    devices = await scanner.discover(timeout=timeout, return_adv=True)
    return [dict(address=device.address, name=advert.local_name or device.name or "",
                 rssi=advert.rssi)
            for device, advert in devices.values()
            if (advert.local_name or device.name or "").startswith("Deck ")]


def readonly_rows(rows):
    """Strip controls even if callers later supply decorated session views."""
    return [dict(row, actions=[], control_token="-", review="", tab="") for row in rows]


class ProbeLink:
    def __init__(self, client, protocol="AD3B"):
        if protocol not in {"AD3", "AD3B"}:
            raise ValueError("Unsupported Bluetooth protocol")
        self.client = client
        self.protocol = protocol
        self.lines = Lines()
        self.incoming = asyncio.Queue(maxsize=256)
        self.credit = 0
        self.credit_changed = asyncio.Event()
        self.error = ""
        self.session = secrets.token_hex(4)
        self.last_test = 0
        self.tests = 0
        self.write_size = 20

    def data_received(self, _sender, data):
        for line in self.lines.feed(data):
            try:
                self.incoming.put_nowait(line)
            except asyncio.QueueFull:
                self.error = "Bluetooth receive queue overflow"
                return

    def flow_received(self, _sender, data):
        if len(data) != 4:
            self.error = "Invalid Bluetooth flow-control value"
        else:
            value = int.from_bytes(data, "big")
            if not 0 < value <= MAX_CREDIT:
                self.error = "Invalid Bluetooth receive credit"
            else:
                self.credit = value
        self.credit_changed.set()

    def check(self):
        if self.error:
            raise RuntimeError(self.error)
        if not self.client.is_connected:
            raise ConnectionError("Flipper Bluetooth disconnected")

    async def send(self, data):
        offset = 0
        while offset < len(data):
            self.check()
            if not self.credit:
                self.credit_changed.clear()
                await asyncio.wait_for(self.credit_changed.wait(), 5)
                continue
            count = min(self.write_size, self.credit, len(data) - offset)
            # Reserve before awaiting: a flow notification can arrive during write.
            self.credit -= count
            await asyncio.wait_for(
                self.client.write_gatt_char(RX, data[offset:offset + count], response=False), 5)
            offset += count

    async def start(self):
        if self.client.services.get_service(SERVICE) is None:
            raise RuntimeError("Expected Flipper serial GATT service is missing")
        rx = self.client.services.get_characteristic(RX)
        if rx is None or "write-without-response" not in rx.properties:
            raise RuntimeError("Expected flow-controlled serial write characteristic is missing")
        self.write_size = min(243, rx.max_write_without_response_size)
        if self.write_size < 1:
            raise RuntimeError("Invalid Bluetooth write size")
        await self.client.start_notify(TX, self.data_received)
        await self.client.start_notify(FLOW, self.flow_received)
        # An initial read also triggers the OS authentication requirement.
        self.flow_received(None, await self.client.read_gatt_char(FLOW))
        self.check()
        await self.send(f"PING\t{self.protocol}\n".encode())
        if await self.receive(5) != f"HELLO\t{self.protocol}\n".encode():
            raise RuntimeError(f"Agent Deck {self.protocol} handshake failed; open the matching Flipper app")

    async def receive(self, timeout):
        deadline = time.monotonic() + timeout
        while True:
            self.check()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise asyncio.TimeoutError()
            line = await asyncio.wait_for(self.incoming.get(), remaining)
            if self.protocol == "AD3":
                return line  # The main bridge validates connection/request tokens.
            if line.startswith(b"TEST\t"):
                fields = line.rstrip(b"\n").split(b"\t")
                if (len(fields) == 3 and fields[1] == self.session.encode() and
                        fields[2].isdigit() and self.last_test < int(fields[2]) < 2**32):
                    self.last_test = int(fields[2])
                    self.tests += 1
                    await self.send(b"TESTED\t" + fields[1] + b"\t" + fields[2] + b"\n")
                    log.info("Harmless Flipper button received (%s); no agent action executed", self.tests)
                continue
            if line.startswith(b"ACT\t"):
                raise RuntimeError("Agent actions are not supported by the Bluetooth probe")
            return line

    async def exchange(self, revision, rows):
        started = time.monotonic()
        await self.send(snapshot(self.session, revision, readonly_rows(rows)))
        expected = f"ACK\t{self.session}\t{revision}\t{len(rows)}\n".encode()
        if await self.receive(5) != expected:
            raise RuntimeError("Snapshot acknowledgement did not match this connection/revision")
        return round((time.monotonic() - started) * 1000, 1)


async def probe(address, duration=30, cycles=1, demo=False, store_path=None):
    client_type, scanner = backend()
    store = None if demo else EventStore(store_path)
    deck = demo_deck() if demo else Deck()
    cursor = 0
    report = dict(address=address, mode="demo" if demo else "live", cycles=[])
    try:
        for cycle in range(cycles):
            log.info("Scanning for selected Flipper (%s), connection %s/%s", address, cycle + 1, cycles)
            device = await scanner.find_device_by_address(address, timeout=10)
            if device is None:
                raise RuntimeError("Selected Flipper not advertising. Open the BLE diagnostic test app on the device.")
            # Explicit device selection, native PIN pairing, no automatic unpair.
            async with client_type(device, pair=True, timeout=60) as client:
                link = ProbeLink(client)
                await link.start()
                log.info("Authenticated AD3B connection established; live agent controls disabled")
                end = time.monotonic() + duration
                timings = []
                revision = 0
                while time.monotonic() < end:
                    if store:
                        cursor = drain(store, deck, cursor)
                        remove_dead(deck)
                    revision += 1
                    rows = deck.view()
                    timings.append(await link.exchange(revision, rows))
                    log.info("Snapshot %s acknowledged: %s agents, %.1f ms", revision, len(rows), timings[-1])
                    # Listen for the test button between snapshots too.
                    try:
                        unexpected = await link.receive(min(1, max(.01, end - time.monotonic())))
                        raise RuntimeError(f"Unexpected BLE message: {unexpected[:80]!r}")
                    except asyncio.TimeoutError:
                        pass
                report["cycles"].append(dict(snapshots=len(timings), button_tests=link.tests,
                                              mtu=client.mtu_size, write_size=link.write_size,
                                              mean_snapshot_ms=round(sum(timings) / len(timings), 1),
                                              max_snapshot_ms=max(timings)))
            if cycle + 1 < cycles:
                log.info("Disconnected normally; reconnecting with a fresh connection token")
                await asyncio.sleep(2)
    finally:
        if store:
            store.close()
    return report


def main(args):
    backend()  # Give the optional-install hint before importing exception types.
    from bleak.exc import BleakError
    try:
        if args.bluetooth_command == "scan":
            result = asyncio.run(scan(args.timeout))
        else:
            result = asyncio.run(probe(args.address, args.duration, args.cycles, args.demo, args.store))
        print(json.dumps(result, indent=2))
    except BleakError as error:
        raise RuntimeError(
            f"Bluetooth: {error}. On Windows, pair the Deck device in Settings first, "
            "using the PIN displayed on the Flipper; then retry.") from error
    except asyncio.TimeoutError as error:
        raise RuntimeError("Bluetooth timed out waiting for the Flipper; reopen the test app and retry.") from error
