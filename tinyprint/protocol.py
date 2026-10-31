import struct
from collections.abc import Iterable
from dataclasses import dataclass

WIDTH = 384
DPI = 200
ROW_BYTES = WIDTH // 8
MAX_PAYLOAD = 4096


def crc8(data: bytes) -> int:
    crc = 0
    for value in data:
        crc ^= value
        for _ in range(8):
            crc = ((crc << 1) ^ (0x07 if crc & 0x80 else 0)) & 0xFF
    return crc


def packet(command: int, payload: bytes = b"\x00", direction: int = 0) -> bytes:
    if len(payload) > MAX_PAYLOAD:
        raise ValueError("Protocol payload is too large")
    return (
        b"\x51\x78" + bytes((command, direction))
        + struct.pack("<H", len(payload)) + payload + bytes((crc8(payload), 0xFF))
    )


@dataclass(frozen=True)
class Frame:
    command: int
    direction: int
    payload: bytes


class FrameParser:
    def __init__(self):
        self.buffer = bytearray()

    def feed(self, data: bytes) -> list[Frame]:
        self.buffer.extend(data)
        frames = []
        while len(self.buffer) >= 6:
            if self.buffer[:2] != b"\x51\x78":
                raise ValueError("Invalid printer protocol header")
            length = int.from_bytes(self.buffer[4:6], "little")
            if length > MAX_PAYLOAD:
                raise ValueError("Invalid printer protocol payload length")
            size = length + 8
            if len(self.buffer) < size:
                break
            payload = bytes(self.buffer[6:6 + length])
            if self.buffer[size - 1] != 0xFF or self.buffer[size - 2] != crc8(payload):
                raise ValueError("Invalid printer protocol CRC or terminator")
            frames.append(Frame(self.buffer[2], self.buffer[3], payload))
            del self.buffer[:size]
        return frames


def page_packets(rows: Iterable[bytes], energy: int = 5000, feed: int = 96):
    if not 1 <= energy <= 8000:
        raise ValueError("Energy must be between 1 and 8000 (X6h limits)")
    if not 0 <= feed <= 1000:
        raise ValueError("Feed must be between 0 and 1000 dots")
    yield packet(0xA4, b"\x33")
    yield packet(0xAF, struct.pack("<H", energy))
    yield packet(0xBE, b"\x00")
    yield packet(0xBD, b"\x0a")
    count = 0
    for row in rows:
        if len(row) != ROW_BYTES:
            raise ValueError(f"Each raster row must contain {ROW_BYTES} bytes")
        count += 1
        yield packet(0xA2, row)
    if not count:
        raise ValueError("Cannot print an empty raster")
    yield packet(0xBD, b"\x19")
    if feed:
        yield packet(0xA1, struct.pack("<H", feed))
    yield packet(0xA3)
