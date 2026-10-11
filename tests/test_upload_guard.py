"""Run: .venv/bin/python tests/test_upload_guard.py   (plain asserts, no test framework needed)"""
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from snapiano_be.upload_guard import MIN_UPLOAD_BYTES, ScanError, decode_upload, sniff

rng = np.random.default_rng(0)
noise = rng.integers(0, 255, (300, 300, 3), dtype=np.uint8)  # noise compresses badly, so the files are comfortably large
jpeg = cv2.imencode(".jpg", noise)[1].tobytes()
png = cv2.imencode(".png", noise)[1].tobytes()
tiny_jpeg = cv2.imencode(".jpg", np.zeros((8, 8, 3), np.uint8))[1].tobytes()


def code_of(data: bytes) -> str:
    try:
        decode_upload(data)
    except ScanError as e:
        return e.code
    return "ok"


cases = {
    "the real failure (14-byte text)": (b"File not found", "bad_upload"),
    "empty file": (b"", "bad_upload"),
    "valid JPEG": (jpeg, "ok"),
    "valid PNG": (png, "ok"),
    "truncated JPEG (half the bytes)": (jpeg[: len(jpeg) // 2], "ok-or-bad"),  # OpenCV may decode a partial JPEG; see below
    "JPEG signature + garbage": (b"\xff\xd8\xff" + bytes(rng.integers(0, 255, 20000, dtype=np.uint8)), "bad_upload"),
    "PNG signature + garbage": (b"\x89PNG\r\n\x1a\n" + bytes(rng.integers(0, 255, 20000, dtype=np.uint8)), "bad_upload"),
    "big non-image (text)": (b"hello " * 5000, "bad_upload"),
    "valid but tiny JPEG (under the guard)": (tiny_jpeg, "bad_upload"),
}
failed = 0
for name, (data, expected) in cases.items():
    got = code_of(data)
    ok = got in ("ok", "bad_upload") if expected == "ok-or-bad" else got == expected
    failed += not ok
    print(f"{'PASS' if ok else 'FAIL'}  {name}: {got}")

# Phone photos are often stored sideways with an EXIF "rotate" tag: the worker must see them upright.
import io
from PIL import Image

for orientation, rotated in ((1, False), (6, True), (8, True)):
    exif = Image.Exif()
    exif[0x0112] = orientation
    buf = io.BytesIO()
    Image.fromarray(noise[:200, :300]).save(buf, "JPEG", exif=exif, quality=95)  # 300 wide, 200 tall as stored
    h, w = decode_upload(buf.getvalue()).shape[:2]
    ok = (w, h) == ((200, 300) if rotated else (300, 200))
    failed += not ok
    print(f"{'PASS' if ok else 'FAIL'}  EXIF orientation {orientation} is applied on decode: {w}x{h}")

assert sniff(jpeg) == "jpeg" and sniff(png) == "png" and sniff(b"GIF89a....") is None
print("guard threshold:", MIN_UPLOAD_BYTES, "bytes (env MIN_UPLOAD_BYTES overrides)")
print("FAILED" if failed else "all passed")
sys.exit(1 if failed else 0)
