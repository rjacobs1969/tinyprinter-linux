import re
import select
import socket
import time
from collections.abc import Iterable

from .protocol import Frame, FrameParser, packet


def validate_address(address: str) -> str:
    if not re.fullmatch(r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", address):
        raise ValueError("A Bluetooth MAC address is required, e.g. AA:BB:CC:DD:EE:FF")
    return address.upper()


class Printer:
    def __init__(self, address: str, channel: int = 1, timeout: float = 30):
        self.address = validate_address(address)
        if not 1 <= channel <= 30:
            raise ValueError("RFCOMM channel must be between 1 and 30")
        if timeout <= 0:
            raise ValueError("Timeout must be positive")
        self.channel = channel
        self.timeout = timeout
        self.socket = None
        self.parser = FrameParser()
        self.paused = False
        self.status = None

    def __enter__(self):
        connection = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)
        connection.settimeout(self.timeout)
        try:
            connection.connect((self.address, self.channel))
        except OSError:
            connection.close()
            raise
        self.socket = connection
        return self

    def __exit__(self, *_):
        if self.socket is not None:
            self.socket.close()
            self.socket = None

    def _read(self, wait: float = 0) -> list[Frame]:
        if self.socket is None:
            raise RuntimeError("Printer is not connected")
        if not select.select([self.socket], [], [], wait)[0]:
            return []
        data = self.socket.recv(4096)
        if not data:
            raise ConnectionError("Printer disconnected")
        frames = self.parser.feed(data)
        for frame in frames:
            if frame.direction != 1:
                continue
            if frame.command == 0xAE:
                if frame.payload == b"\x10":
                    self.paused = True
                elif frame.payload == b"\x00":
                    self.paused = False
                else:
                    raise ValueError("Unknown printer flow-control response")
            if frame.command == 0xA3:
                if not frame.payload:
                    raise ValueError("Empty printer status")
                self.status = frame.payload[0]
                self._check_status()
        return frames

    def _check_status(self):
        errors = []
        for bit, message in ((1, "out of paper"), (2, "cover open"),
                             (4, "overheated"), (8, "battery too low")):
            if self.status is not None and self.status & bit:
                errors.append(message)
        if errors:
            raise RuntimeError("Printer: " + ", ".join(errors))

    def query(self, command: int) -> Frame:
        if self.socket is None:
            raise RuntimeError("Printer is not connected")
        self.socket.sendall(packet(command))
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            frames = self._read(min(0.2, max(0, deadline - time.monotonic())))
            for frame in frames:
                if frame.command == command and frame.direction == 1:
                    return frame
        raise TimeoutError(f"No response to printer command 0x{command:02x}")

    def send(self, chunks: Iterable[bytes]):
        if self.socket is None:
            raise RuntimeError("Printer is not connected")
        self.query(0xA3)
        for chunk in chunks:
            for offset in range(0, len(chunk), 180):
                self._read()
                deadline = time.monotonic() + self.timeout
                while self.paused:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("Printer did not resume after buffer-full notification")
                    self._read(min(0.2, remaining))
                self.socket.sendall(chunk[offset:offset + 180])
                time.sleep(0.004)
        # Do not disconnect while the printer is still consuming its buffer.
        deadline = time.monotonic() + max(self.timeout, 120)
        while time.monotonic() < deadline:
            status = self.query(0xA3).payload[0]
            if not status & 0x80 and not self.paused:
                return
            time.sleep(0.2)
        raise TimeoutError("Printer did not finish before the completion timeout")
