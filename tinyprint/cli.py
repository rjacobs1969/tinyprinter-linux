import argparse
import json
import sys
from pathlib import Path

from .protocol import page_packets
from .render import document_pages
from .transport import Printer


def main():
    parser = argparse.ArgumentParser(description="X6h monochrome printing over Bluetooth Classic")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("info", "print"):
        command = commands.add_parser(name)
        command.add_argument("--address", required=True)
        command.add_argument("--channel", type=int, default=1)
        if name == "print":
            command.add_argument("file", type=Path)
            command.add_argument("--height-mm", type=float, default=100)
            command.add_argument("--energy", type=int, default=5000)
            command.add_argument("--feed", type=int, default=96, help="Tear-off feed, in dots")
            command.add_argument("--keep-blank", action="store_true")
    encode = commands.add_parser("encode", help="Render a document to protocol bytes without connecting")
    encode.add_argument("file", type=Path)
    encode.add_argument("output", type=Path)
    encode.add_argument("--height-mm", type=float, default=100)
    encode.add_argument("--energy", type=int, default=5000)
    encode.add_argument("--feed", type=int, default=96)
    encode.add_argument("--keep-blank", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "info":
            with Printer(args.address, args.channel) as printer:
                info = printer.query(0xA8)
                status = printer.query(0xA3)
                print(json.dumps({"address": printer.address, "channel": printer.channel,
                                  "info_hex": info.payload.hex(), "status_hex": status.payload.hex()}))
        else:
            chunks = (
                chunk
                for page in document_pages(args.file, args.height_mm, not args.keep_blank)
                for chunk in page_packets(page, args.energy, args.feed)
            )
            if args.command == "encode":
                with args.output.open("xb") as output:
                    for chunk in chunks:
                        output.write(chunk)
            else:
                with Printer(args.address, args.channel) as printer:
                    printer.send(chunks)
    except (OSError, ValueError, RuntimeError) as error:
        print(f"tinyprint: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
