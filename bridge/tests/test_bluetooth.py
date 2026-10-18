import asyncio
import unittest
import time
from unittest.mock import patch

from agent_deck.bluetooth import FLOW, RX, TX, ProbeLink, readonly_rows
from agent_deck.transport import Lines


class Services:
    def __init__(self, device):
        self.device = device

    def get_service(self, _uuid):
        return object()

    def get_characteristic(self, _uuid):
        return type("Characteristic", (), dict(properties=["write-without-response"],
                    max_write_without_response_size=self.device.mtu_size - 3))()


class Device:
    """Authenticated GATT peer with the firmware's 486-byte flow-control window."""
    def __init__(self):
        self.is_connected = True
        self.mtu_size = 23
        self.services = Services(self)
        self.callbacks = {}
        self.lines = Lines()
        self.credit = 486
        self.writes = []
        self.received = []
        self.session = None
        self.count = None
        self.bad_ack = False

    async def start_notify(self, uuid, callback):
        self.callbacks[uuid] = callback

    async def read_gatt_char(self, _uuid):
        return (486).to_bytes(4, "big")

    def reply(self, line):
        # Indications can fragment a line at arbitrary byte boundaries.
        for offset in range(0, len(line), 7):
            self.callbacks[TX](None, line[offset:offset + 7])

    async def write_gatt_char(self, uuid, data, response):
        assert uuid == RX and not response and 0 < len(data) <= min(243, self.mtu_size - 3)
        assert len(data) <= self.credit, "host overran the advertised receive window"
        self.credit -= len(data)
        self.writes.append(bytes(data))
        for line in self.lines.feed(data):
            self.received.append(line)
            fields = line.decode().strip().split("\t")
            if fields[0] == "PING":
                self.reply(f"HELLO\t{fields[1]}\n".encode())
            elif fields[0] == "BEGIN":
                self.session, self.count = fields[1], fields[3]
            elif fields[0] == "END":
                session = "badbeef0" if self.bad_ack else self.session
                self.reply(f"ACK\t{session}\t{fields[1]}\t{self.count}\n".encode())
        if self.credit == 0:
            self.credit = 486
            # Arrives during the awaited write, before the host returns from it.
            self.callbacks[FLOW](None, self.credit.to_bytes(4, "big"))


class BluetoothTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.device = Device()
        self.link = ProbeLink(self.device)
        await self.link.start()

    async def test_fragmented_full_snapshot_and_multiple_flow_windows(self):
        rows = [dict(id=f"agent{i}", label="Agent " + str(i), state="waiting",
                     event="1234abcd", session_id="s" * 128, project="p" * 256,
                     actions=["approve", "reject"], control_token="1234abcd", review="SECRET")
                for i in range(16)]
        await self.link.exchange(12, rows)
        self.assertGreater(len(b"".join(self.device.writes)), 486 * 10)
        controls = [line for line in self.device.received if line.startswith(b"CTRL\t")]
        self.assertEqual(len(controls), 16)
        self.assertTrue(all(line.endswith(b"\t0\t-\n") for line in controls))
        self.assertNotIn(b"SECRET", b"".join(self.device.writes))

    async def test_button_echo_is_current_and_not_replayed(self):
        current = self.link.session.encode()
        for line in (b"TEST\toldnonce\t100\n", b"TEST\t" + current + b"\t1\n",
                     b"TEST\t" + current + b"\t1\n", b"TEST\t" + current + b"\t0\n"):
            self.device.reply(line)
        self.device.reply(b"sentinel\n")
        self.assertEqual(await self.link.receive(1), b"sentinel\n")
        self.assertEqual(self.link.tests, 1)
        self.assertEqual(self.device.received.count(b"TESTED\t" + current + b"\t1\n"), 1)

    async def test_negotiated_mtu_and_credit_boundary(self):
        self.device = Device()
        self.device.mtu_size = 247
        self.link = ProbeLink(self.device)
        await self.link.start()
        self.device.writes.clear()
        await self.link.send(b"x" * 1200)
        self.assertEqual(b"".join(self.device.writes), b"x" * 1200)
        self.assertEqual(max(map(len, self.device.writes)), 243)

    async def test_wrong_connection_ack_rejected(self):
        self.device.bad_ack = True
        with self.assertRaisesRegex(RuntimeError, "acknowledgement"):
            await self.link.exchange(1, [])

    async def test_agent_action_is_never_dispatched(self):
        self.device.reply(b"ACT\t1234abcd\t1\tagent1\tapprove\t1234abcd\n")
        before = len(self.device.writes)
        with self.assertRaisesRegex(RuntimeError, "not supported"):
            await self.link.receive(1)
        self.assertEqual(len(self.device.writes), before)

    async def test_disconnect_and_bad_credit_fail_closed(self):
        self.link.flow_received(None, b"\0")
        with self.assertRaisesRegex(RuntimeError, "flow-control"):
            await self.link.send(b"x")
        self.link.error = ""
        self.device.is_connected = False
        with self.assertRaises(ConnectionError):
            await self.link.send(b"x")

    async def test_receive_queue_is_bounded(self):
        self.link.data_received(None, b"x\n" * 257)
        self.assertEqual(self.link.incoming.qsize(), 256)
        with self.assertRaisesRegex(RuntimeError, "overflow"):
            await self.link.receive(1)

    def test_readonly_rows_does_not_modify_original(self):
        row = dict(id="a", actions=["continue"], tab="tab-id", review="command")
        result = readonly_rows([row])[0]
        self.assertEqual(result["actions"], [])
        self.assertEqual(result["control_token"], "-")
        self.assertEqual(row["actions"], ["continue"])


class ManagedDevice(Device):
    async def connect(self):
        self.is_connected = True

    async def disconnect(self):
        self.is_connected = False


class Scanner:
    async def find_device_by_address(self, address, timeout):
        return address


class BluetoothLinkTests(unittest.TestCase):
    def setUp(self):
        from agent_deck.ble_link import BluetoothLink
        self.devices = []
        def factory(*args, **kwargs):
            device = ManagedDevice()
            self.devices.append(device)
            return device
        self.patch = patch("agent_deck.ble_link.backend", return_value=(factory, Scanner()))
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.link = BluetoothLink("80:E1:27:37:4B:48")
        self.addCleanup(self.link.shutdown)
        self.wait_connected()

    def wait_connected(self):
        until = time.monotonic() + 2
        while time.monotonic() < until:
            if self.link.connect():
                return
            time.sleep(.005)
        self.fail(self.link.error or "Bluetooth did not connect")

    def receive(self):
        until = time.monotonic() + 2
        while time.monotonic() < until:
            lines = self.link.receive()
            if lines:
                return lines
            time.sleep(.005)
        self.fail("No Bluetooth lines received")

    def test_main_protocol_passes_current_action_for_existing_validation(self):
        device = self.devices[-1]
        self.assertIn(b"PING\tAD3\n", device.received)
        action = b"ACT\t1234abcd\t1\tagent\tapprove\tabcd1234\n"
        self.link.loop.call_soon_threadsafe(device.reply, action)
        self.assertEqual([action], self.receive())

    def test_writes_are_serial_and_reconnect_discards_old_input(self):
        self.link.send(b"one\n")
        self.link.send(b"two\n")
        until = time.monotonic() + 1
        while self.link.busy and time.monotonic() < until:
            time.sleep(.005)
        self.link.receive()
        self.assertEqual([b"one\n", b"two\n"], self.devices[-1].received[-2:])
        self.link.incoming.put_nowait(b"old action\n")
        self.link.close()
        self.link.closing.result(timeout=2)
        self.link.next_attempt = 0
        self.wait_connected()
        self.assertEqual([], self.link.receive())
        self.assertEqual(2, len(self.devices))

    def test_connection_failure_becomes_disconnected_and_recovers(self):
        self.devices[-1].is_connected = False
        self.assertFalse(self.link.connected)
        self.link.next_attempt = 0
        self.wait_connected()
        self.assertTrue(self.link.connected)
        self.assertEqual(2, len(self.devices))
