import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageOps

from .protocol import DPI, WIDTH

PAPER_MM = 57
REVERSE_BITS = bytes(int(f"{value:08b}"[::-1], 2) for value in range(256))


def raster_rows(image: Image.Image, trim: bool = True) -> list[bytes]:
    if image.width != WIDTH:
        raise ValueError(f"Raster width must be {WIDTH} dots")
    if image.height > 40000:
        raise ValueError("A page cannot exceed 40000 dots in height")
    if image.mode != "1":
        image = image.convert("1")
    if trim:
        bounds = ImageOps.invert(image.convert("L")).getbbox()
        image = image.crop((0, 0, WIDTH, bounds[3] if bounds else 1))
    # Pillow: white=1, leftmost pixel=MSB. X6h: black=1, leftmost=LSB.
    data = bytes(value ^ 0xFF for value in image.tobytes()).translate(REVERSE_BITS)
    stride = WIDTH // 8
    return [data[start:start + stride] for start in range(0, len(data), stride)]


def image_page(path: Path, trim: bool = True) -> list[bytes]:
    with Image.open(path) as original:
        image = ImageOps.exif_transpose(original).convert("RGBA")
        white = Image.new("RGBA", image.size, "white")
        white.alpha_composite(image)
        height = max(1, round(image.height * WIDTH / image.width))
        if height > 40000:
            raise ValueError("Image is too tall")
        image = white.convert("L").resize((WIDTH, height), Image.Resampling.LANCZOS)
        return raster_rows(image, trim)


def pdf_pages(path: Path, height_mm: float = 100, trim: bool = True):
    if not 10 <= height_mm <= 5000:
        raise ValueError("Page height must be between 10 and 5000 mm")
    with tempfile.TemporaryDirectory(prefix="tinyprint-render-") as temporary:
        pattern = str(Path(temporary) / "page-%06d.pbm")
        result = subprocess.run(
            ["gs", "-q", "-dSAFER", "-dBATCH", "-dNOPAUSE", "-dFIXEDMEDIA",
             "-dPDFFitPage", "-sDEVICE=pbmraw", f"-r{DPI}",
             f"-dDEVICEWIDTHPOINTS={PAPER_MM * 72 / 25.4}",
             f"-dDEVICEHEIGHTPOINTS={height_mm * 72 / 25.4}",
             f"-sOutputFile={pattern}", "-f", str(path.resolve())],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, check=False,
        )
        if result.returncode:
            raise RuntimeError("Ghostscript failed: " + result.stderr.strip())
        pages = sorted(Path(temporary).glob("page-*.pbm"))
        if not pages:
            raise ValueError("PDF contains no printable pages")
        for page in pages:
            with Image.open(page) as image:
                left = (image.width - WIDTH) // 2
                yield raster_rows(image.crop((left, 0, left + WIDTH, image.height)), trim)


def document_pages(path: Path, height_mm: float = 100, trim: bool = True):
    with path.open("rb") as source:
        is_pdf = source.read(5) == b"%PDF-"
    if is_pdf:
        yield from pdf_pages(path, height_mm, trim)
    else:
        yield image_page(path, trim)
