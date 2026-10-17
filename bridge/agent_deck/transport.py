"""USB CDC channel 1, with an application handshake and bounded framing."""
from __future__ import annotations

import logging
import time

import serial
from serial.tools import list_ports

from .protocol import MAX_LINE

log = logging.getLogger(__name__)


class Lines:
    def __init__(self):
        self.buffer = bytearray()
        self.dropping = False

    def feed(self, data):
        result = []
        for byte in data:
            if byte == 10:
                if self.buffer and not self.dropping:
                    result.append(bytes(self.buffer) + b"\n")
                self.buffer.clear()
                self.dropping = False
            elif not self.dropping:
                if len(self.buffer) >= MAX_LINE - 1 or byte == 0:
                    self.dropping = True
                    self.buffer.clear()
                else:
                    self.buffer.append(byte)
        return result


def flipper_ports():
    return sorted([p for p in list_ports.comports() if p.vid == 0x0483 and p.pid == 0x5740],
                  key=lambda p: p.device, reverse=True)


class SerialLink:
    def __init__(self, port="auto"):
        self.requested = port
        self.port = None
        self.lines = Lines()
        self.next_attempt = 0
        self.last_ack = 0

    def connect(self):
        if self.port:
            return True
        if time.monotonic() < self.next_attempt:
            return False
        self.next_attempt = time.monotonic() + 2
        candidates = [p.device for p in flipper_ports()] if self.requested == "auto" else [self.requested]
        for name in candidates:
            candidate = None
            try:
                candidate = serial.Serial(name, 115200, timeout=0.05, write_timeout=0.5)
                candidate.reset_input_buffer()
                candidate.write(b"PING\tAD3\n")
                lines = Lines()
                deadline = time.monotonic() + 0.8
                while time.monotonic() < deadline:
                    if b"HELLO\tAD3\n" in lines.feed(candidate.read(candidate.in_waiting or 1)):
                        self.port, self.lines = candidate, lines
                        self.last_ack = time.monotonic()
                        log.info("Connected to Agent Deck on %s", name)
                        return True
            except (serial.SerialException, OSError):
                pass
            if candidate:
                candidate.close()
        return False

    def send(self, data):
        if self.port is None or self.port.write(data) != len(data):
            raise serial.SerialException("Incomplete USB write")

    def receive(self):
        return self.lines.feed(self.port.read(self.port.in_waiting or 1))

    def close(self):
        if self.port:
            self.port.close()
            self.port = None
        self.lines = Lines()
