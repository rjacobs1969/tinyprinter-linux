import io
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from tinyprint.cups import cups_options, filter_job, page_height, parse_uri, validate_job
from tinyprint.protocol import FrameParser, crc8, packet, page_packets
from tinyprint.render import image_page, pdf_pages, raster_rows
from tinyprint.transport import Printer


class ProtocolTests(unittest.TestCase):
    def test_captured_packets(self):
        self.assertEqual(packet(0xA8).hex(), "5178a80001000000ff")
        self.assertEqual(crc8(b"123456789"), 0xF4)
        response = bytes.fromhex("5178a3010300001529c9ff")
        parser = FrameParser()
        self.assertEqual(parser.feed(response[:7]), [])
        frame, = parser.feed(response[7:])
        self.assertEqual(frame.payload, bytes.fromhex("001529"))
        self.assertEqual(frame.direction, 1)

    def test_multiple_frames_and_corruption(self):
        parser = FrameParser()
        self.assertEqual(len(parser.feed(packet(0xAE, b"\x10", 1) + packet(0xAE, direction=1))), 2)
        broken = bytearray(packet(0xA3))
        broken[-2] ^= 1
        with self.assertRaises(ValueError):
            FrameParser().feed(broken)
        with self.assertRaises(ValueError):
            FrameParser().feed(b"\x51\x78\xa2\x00\xff\xff")

    def test_raster_bit_order_white_padding_and_trim(self):
        image = Image.new("1", (384, 4), 1)
        image.putpixel((0, 0), 0)
        image.putpixel((7, 0), 0)
        image.putpixel((8, 0), 0)
        image.putpixel((383, 1), 0)
        rows = raster_rows(image)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][:3], b"\x81\x01\x00")
        self.assertEqual(rows[1][-1], 0x80)
        self.assertEqual(len(raster_rows(image, trim=False)), 4)
        self.assertEqual(raster_rows(Image.new("1", (384, 2), 1)), [bytes(48)])

    def test_job_validation_before_bluetooth(self):
        chunks = list(page_packets([bytes(48)]))
        validate_job(io.BytesIO(b"".join(chunks)))
        frames = FrameParser().feed(b"".join(chunks))
        self.assertEqual([frame.command for frame in frames], [0xA4, 0xAF, 0xBE, 0xBD, 0xA2, 0xBD, 0xA1, 0xA3])
        self.assertEqual(frames[1].payload, (5000).to_bytes(2, "little"))
        for invalid in (b"".join(chunks)[:-1], packet(0xA9), packet(0xAF, b"\xff\xff") + chunks[4]):
            with self.subTest(invalid=invalid[:12]), self.assertRaises(ValueError):
                validate_job(io.BytesIO(invalid))
        for energy in (0, 8001):
            with self.assertRaises(ValueError):
                list(page_packets([bytes(48)], energy=energy))


class TransportTests(unittest.TestCase):
    def test_pause_resume_split_notifications(self):
        printer = Printer("AA:BB:CC:DD:EE:FF")
        frames = [packet(0xAE, b"\x10", 1), packet(0xAE, b"\x00", 1)]
        with patch("tinyprint.transport.select.select", return_value=([1], [], [])):
            from unittest.mock import Mock
            printer.socket = Mock()
            printer.socket.recv.side_effect = [frames[0][:5], frames[0][5:], frames[1]]
            printer._read()
            self.assertFalse(printer.paused)
            printer._read()
            self.assertTrue(printer.paused)
            printer._read()
            self.assertFalse(printer.paused)

    def test_status_checks_combined_bits_and_disconnect(self):
        printer = Printer("AA:BB:CC:DD:EE:FF")
        printer.status = 0x13
        with self.assertRaisesRegex(RuntimeError, "out of paper, cover open"):
            printer._check_status()
        printer.status = 0x90
        printer._check_status()
        from unittest.mock import Mock
        printer.socket = Mock()
        printer.socket.recv.return_value = b""
        with patch("tinyprint.transport.select.select", return_value=([1], [], [])):
            with self.assertRaises(ConnectionError):
                printer._read()


class CupsTests(unittest.TestCase):
    def test_custom_sizes_and_uri(self):
        self.assertEqual(page_height({"PageSize": "Custom.57x180mm"}), 180)
        self.assertAlmostEqual(page_height({"PageSize": "Custom.161.575x510.236pt"}), 180, places=2)
        self.assertEqual(page_height({"PageSize": "Roll50"}), 50)
        self.assertEqual(cups_options("PageSize=Roll150 Darkness=Dark")["Darkness"], "Dark")
        self.assertEqual(parse_uri("tinyprint:///aa:bb:cc:dd:ee:ff?channel=1"), ("AA:BB:CC:DD:EE:FF", 1))
        self.assertEqual(parse_uri("tinyprint:///AA:BB:CC:DD:EE:FF"), ("AA:BB:CC:DD:EE:FF", 1))
        for uri in ("http://AA:BB:CC:DD:EE:FF", "tinyprint://oops",
                    "tinyprint://AA:BB:CC:DD:EE:FF?channel=1",
                    "tinyprint:///AA:BB:CC:DD:EE:FF?channel=1&channel=2",
                    "tinyprint:///AA:BB:CC:DD:EE:FF/extra"):
            with self.assertRaises(ValueError):
                parse_uri(uri)
        with self.assertRaises(ValueError):
            page_height({"PageSize": "Custom.80x180mm"})

    def test_uri_accepted_by_cups(self):
        import ctypes
        import ctypes.util

        library = ctypes.util.find_library("cups")
        if library is None:
            self.skipTest("CUPS library is not installed")
        separate = ctypes.CDLL(library).httpSeparateURI
        separate.argtypes = [
            ctypes.c_int, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int,
            ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
            ctypes.POINTER(ctypes.c_int), ctypes.c_char_p, ctypes.c_int,
        ]
        separate.restype = ctypes.c_int
        scheme, user, host, resource = [ctypes.create_string_buffer(1024) for _ in range(4)]
        port = ctypes.c_int()
        uri = "tinyprint:///AA:BB:CC:DD:EE:FF?channel=1"
        result = separate(15, uri.encode(), scheme, 1024, user, 1024, host, 1024,
                          ctypes.byref(port), resource, 1024)
        self.assertGreaterEqual(result, 0)
        self.assertEqual(host.value, b"")
        self.assertEqual(resource.value, b"/AA:BB:CC:DD:EE:FF?channel=1")
        self.assertEqual(parse_uri(uri), ("AA:BB:CC:DD:EE:FF", 1))

    def test_pdf_render_filter_copies_and_transparent_image(self):
        with tempfile.TemporaryDirectory() as temporary:
            pdf = Path(temporary) / "two pages.pdf"
            source = b"""%!PS
<</PageSize [161.575 141.732]>> setpagedevice
0 setgray 12 100 10 10 rectfill showpage
0 setgray 30 100 10 10 rectfill showpage
"""
            subprocess.run(["gs", "-q", "-dSAFER", "-dBATCH", "-dNOPAUSE", "-sDEVICE=pdfwrite",
                            f"-sOutputFile={pdf}", "-"], input=source, check=True)
            pages = list(pdf_pages(pdf, height_mm=50))
            self.assertEqual(len(pages), 2)
            self.assertTrue(any(any(row) for row in pages[0]))
            from unittest.mock import Mock
            output = io.BytesIO()
            stdout = Mock(buffer=output)
            with patch("tinyprint.cups.sys.stdout", stdout):
                self.assertEqual(filter_job(["filter", "1", "user", "title", "2", "PageSize=Roll50", str(pdf)]), 0)
            frames = FrameParser().feed(output.getvalue())
            self.assertEqual(sum(frame.command == 0xAF for frame in frames), 4)
            validate_job(io.BytesIO(output.getvalue()))
            png = Path(temporary) / "transparent.png"
            Image.new("RGBA", (384, 10), (0, 0, 0, 0)).save(png)
            self.assertEqual(image_page(png), [bytes(48)])


if __name__ == "__main__":
    unittest.main()
