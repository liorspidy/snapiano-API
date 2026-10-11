"""Run: .venv/bin/python tests/test_pixel_guard.py"""
import io
import struct
import sys
import zlib
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from snapiano_be import upload_guard as ug  # noqa: E402

failed = 0


def check(name: str, ok: bool, extra: str = "") -> None:
    global failed
    failed += not ok
    print(f"{'PASS' if ok else 'FAIL'}  {name}{(': ' + extra) if extra else ''}")


def code_of(data: bytes) -> str:
    try:
        ug.decode_upload(data)
    except ug.ScanError as e:
        return e.code
    return "ok"


rng = np.random.default_rng(3)
noise = rng.integers(0, 255, (200, 300, 3), dtype=np.uint8)  # 300 wide x 200 tall


def pil_jpeg(w: int, h: int, **kw) -> bytes:
    b = io.BytesIO()
    Image.fromarray(np.full((h, w, 3), 200, np.uint8)).save(b, "JPEG", **kw)
    return b.getvalue()


# ---- reading the size from the header only
png = cv2.imencode(".png", noise)[1].tobytes()
jpg = cv2.imencode(".jpg", noise)[1].tobytes()
prog = io.BytesIO(); Image.fromarray(noise).save(prog, "JPEG", progressive=True, quality=90)
exif = Image.Exif(); exif[0x0112] = 6
withexif = io.BytesIO(); Image.fromarray(noise).save(withexif, "JPEG", exif=exif)
check("PNG size", ug.image_dimensions(png) == (300, 200))
check("baseline JPEG size", ug.image_dimensions(jpg) == (300, 200))
check("progressive JPEG size", ug.image_dimensions(prog.getvalue()) == (300, 200))
check("JPEG with EXIF size (stored, before rotation)", ug.image_dimensions(withexif.getvalue()) == (300, 200))
for page in ("BachInvention5.jpg", "allegretto.png", "moonlight_p1.png"):
    f = HERE.parent / "pages" / page
    if f.exists():
        h, w = cv2.imread(str(f)).shape[:2]
        check(f"real page {page} size matches OpenCV", ug.image_dimensions(f.read_bytes()) == (w, h), f"{w}x{h}")

# ---- the guard
def fake_png(w: int, h: int) -> bytes:
    """A PNG header that claims w x h, followed by filler: it would be expensive to decode if believed."""
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    chunk = struct.pack(">I", 13) + b"IHDR" + ihdr + struct.pack(">I", zlib.crc32(b"IHDR" + ihdr))
    return b"\x89PNG\r\n\x1a\n" + chunk + b"\0" * 20000


calls = []
real_imdecode = cv2.imdecode
cv2.imdecode = lambda *a, **k: calls.append(1) or real_imdecode(*a, **k)

check("PNG claiming 50000x50000 is refused as image_too_large", code_of(fake_png(50000, 50000)) == "image_too_large")
check("...and was refused before any decoding", not calls)
big = pil_jpeg(6000, 6000, quality=50)  # 36 MP, a valid and small file
ug.MAX_IMAGE_PIXELS = 10_000_000
check("valid 36 MP JPEG refused when the limit is 10 MP", code_of(big) == "image_too_large")
check("...also before decoding", not calls)
ug.MAX_IMAGE_PIXELS = 100_000_000
check("the same 36 MP size is within the default 100 MP limit", 6000 * 6000 <= ug.MAX_IMAGE_PIXELS)

big_noise = cv2.imencode(".png", noise)[1].tobytes()
ug.MAX_IMAGE_PIXELS = 300 * 200
check("exactly at the limit is accepted", code_of(big_noise) == "ok")
ug.MAX_IMAGE_PIXELS = 300 * 200 - 1
check("one pixel over the limit is refused", code_of(big_noise) == "image_too_large")
ug.MAX_IMAGE_PIXELS = 100_000_000

# ---- unreadable headers fail closed with bad_upload, not a crash
check("JPEG cut before its frame header", code_of(jpg[:60] + b"\0" * 20000) == "bad_upload")
check("PNG without an IHDR chunk", code_of(b"\x89PNG\r\n\x1a\n" + b"\0" * 20000) == "bad_upload")
check("zero-sized PNG", code_of(fake_png(0, 100)) == "bad_upload")
check("normal images still decode", code_of(png) == "ok" and code_of(jpg) == "ok" and code_of(prog.getvalue()) == "ok")

cv2.imdecode = real_imdecode
print("FAILED" if failed else "all passed")
sys.exit(1 if failed else 0)
