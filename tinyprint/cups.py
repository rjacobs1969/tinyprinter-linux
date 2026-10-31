import os
import shlex
import sys
import tempfile
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .protocol import FrameParser, ROW_BYTES, page_packets
from .render import pdf_pages
from .transport import Printer, validate_address


def cups_options(text: str) -> dict[str, str]:
    return dict(item.split("=", 1) for item in shlex.split(text) if "=" in item)


def page_height(options: dict[str, str]) -> float:
    name = options.get("PageSize", "Roll100")
    sizes = {"Roll50": 50, "Roll100": 100, "Roll150": 150, "Roll200": 200, "Roll300": 300}
    if name in sizes:
        return sizes[name]
    if name.startswith("Custom."):
        dimensions = name[7:]
        factor = 25.4 / 72
        for suffix, units in (("mm", 1), ("cm", 10), ("in", 25.4), ("pt", 25.4 / 72)):
            if dimensions.endswith(suffix):
                dimensions = dimensions[:-len(suffix)]
                factor = units
                break
        width, height = (float(value) * factor for value in dimensions.split("x"))
        if abs(width - 57) > 0.5:
            raise ValueError("Custom paper must be 57 mm wide")
        if not 10 <= height <= 5000:
            raise ValueError("Custom paper height must be between 10 and 5000 mm")
        return height
    raise ValueError(f"Unsupported paper size: {name}")


def filter_job(argv: list[str]) -> int:
    if len(argv) not in (6, 7):
        raise ValueError("CUPS filter expects job-id user title copies options [file]")
    copies = int(argv[4])
    if not 1 <= copies <= 100:
        raise ValueError("Copies must be between 1 and 100")
    options = cups_options(argv[5])
    height = page_height(options)
    energy = {"Normal": 5000, "Light": 4250, "Dark": 5750}[options.get("Darkness", "Normal")]
    trim = options.get("Trim", "True") == "True"
    # Complete rendering first: a failed PDF must not leave a partial print job.
    with tempfile.TemporaryDirectory(prefix="tinyprint-filter-") as temporary:
        source = Path(argv[6]) if len(argv) == 7 else Path(temporary) / "input.pdf"
        if len(argv) == 6:
            with source.open("wb") as output:
                copy_stream(sys.stdin.buffer, output)
        spool = Path(temporary) / "job.bin"
        with spool.open("wb") as output:
            for index, page in enumerate(pdf_pages(source, height, trim), 1):
                print(f"PAGE: {index} {copies}", file=sys.stderr)
                for chunk in page_packets(page, energy):
                    output.write(chunk)
        for _ in range(copies):
            with spool.open("rb") as data:
                copy_stream(data, sys.stdout.buffer)
    return 0


def copy_stream(source, destination):
    total = 0
    while chunk := source.read(65536):
        total += len(chunk)
        if total > 100 * 1024 * 1024:
            raise ValueError("Print job exceeds 100 MiB")
        destination.write(chunk)


def parse_uri(uri: str) -> tuple[str, int]:
    parsed = urlparse(uri)
    if parsed.scheme != "tinyprint" or parsed.netloc or parsed.fragment:
        raise ValueError("Expected tinyprint:///MAC-ADDRESS?channel=1")
    options = parse_qs(parsed.query, strict_parsing=True)
    if set(options) - {"channel"} or len(options.get("channel", ["1"])) != 1:
        raise ValueError("Invalid tinyprint URI options")
    return validate_address(parsed.path.removeprefix("/")), int(options.get("channel", ["1"])[0])


def validate_job(source):
    parser = FrameParser()
    rows = 0
    lengths = {0xA4: 1, 0xAF: 2, 0xBE: 1, 0xBD: 1, 0xA2: ROW_BYTES, 0xA1: 2, 0xA3: 1}
    while chunk := source.read(65536):
        for frame in parser.feed(chunk):
            if frame.direction != 0 or lengths.get(frame.command) != len(frame.payload):
                raise ValueError("Job contains an unsupported printer command")
            if frame.command == 0xAF and not 1 <= int.from_bytes(frame.payload, "little") <= 8000:
                raise ValueError("Job contains unsafe print energy")
            if frame.command == 0xA1 and int.from_bytes(frame.payload, "little") > 1000:
                raise ValueError("Job contains excessive paper feed")
            rows += frame.command == 0xA2
    if parser.buffer or not rows:
        raise ValueError("Truncated or empty print job")


def backend_job(argv: list[str]) -> int:
    if len(argv) == 1:
        print('direct tinyprint "Tiny Print X6h" "X6h Bluetooth Classic printer"')
        return 0
    if len(argv) not in (6, 7):
        raise ValueError("CUPS backend expects job-id user title copies options [file]")
    address, channel = parse_uri(os.environ.get("DEVICE_URI", ""))
    with tempfile.TemporaryFile() as spool:
        if len(argv) == 7:
            with open(argv[6], "rb") as source:
                copy_stream(source, spool)
        else:
            copy_stream(sys.stdin.buffer, spool)
        spool.seek(0)
        validate_job(spool)
        spool.seek(0)
        print(f"INFO: Connecting to X6h at {address}", file=sys.stderr)
        with Printer(address, channel) as printer:
            printer.send(iter(lambda: spool.read(180), b""))
    print("INFO: Print job completed", file=sys.stderr)
    return 0


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        if mode == "filter":
            return filter_job(sys.argv[1:])
        if mode == "backend":
            return backend_job(sys.argv[1:])
        raise ValueError("Expected filter or backend mode")
    except (OSError, ValueError, RuntimeError, KeyError) as error:
        print(f"ERROR: tinyprint: {error}", file=sys.stderr)
        # STOP, not RETRY: bytes may already have printed; retrying duplicates them.
        return 4 if mode == "backend" else 1


if __name__ == "__main__":
    sys.exit(main())
