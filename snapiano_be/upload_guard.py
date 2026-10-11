"""Checks that an uploaded scan really is an image the pipeline can use, and the stable error codes the app understands.

Codes (stored in scans.error and mapped to messages in the app; raw details only go to the server log):
  bad_upload    the file is empty/tiny, not a JPEG/PNG, its size cannot be read, or it cannot be decoded
  image_too_large  the picture has more pixels than MAX_IMAGE_PIXELS (refused before it is decoded)
  no_staves     the page was cleaned but no staff lines were found
  no_notes      homr ran but returned no notes
  homr_failed   homr crashed (stderr tail is in the server log)
  homr_timeout  homr took too long
  unexpected    anything else (traceback is in the server log)
"""
from __future__ import annotations

import os

import cv2
import numpy as np

# Initial guard against empty or truncated uploads, not proof of validity: the signature check and a real decode are.
MIN_UPLOAD_BYTES = int(os.environ.get("MIN_UPLOAD_BYTES", 10 * 1024))
# Decoding needs about 3 bytes per pixel (more while converting), so a file that claims a huge picture is refused first.
# 100 megapixels covers today's phone cameras (12 to 50 MP, 200 MP sensors normally save binned photos) and is about 300 MB
# decoded. Raise or lower it with the environment variable if real photos or the server's memory call for it.
MAX_IMAGE_PIXELS = int(os.environ.get("MAX_IMAGE_PIXELS", 100_000_000))


class ScanError(Exception):
    """A failure with a stable code for the app and a detail string for the server log only."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


def sniff(data: bytes) -> str | None:
    """'jpeg' or 'png' from the first bytes, else None. File names and content types are never trusted."""
    if data[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    return None


_SOF = set(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}  # start-of-frame markers; C4, C8 and CC are not frames


def image_dimensions(data: bytes) -> tuple[int, int] | None:
    """(width, height) read from the file header only, without decoding. None if it cannot be found."""
    kind = sniff(data)
    if kind == "png":
        if len(data) >= 24 and data[12:16] == b"IHDR":
            return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
        return None
    if kind == "jpeg":
        i, n = 2, len(data)
        while i + 4 <= n:
            if data[i] != 0xFF:
                i += 1
                continue
            while i < n and data[i] == 0xFF:  # fill bytes
                i += 1
            if i >= n:
                return None
            marker = data[i]
            i += 1
            if marker in (0x01, 0xD8) or 0xD0 <= marker <= 0xD7:  # markers without a length
                continue
            if marker in (0xD9, 0xDA) or i + 2 > n:  # end of image or start of scan before any frame header
                return None
            length = int.from_bytes(data[i:i + 2], "big")
            if marker in _SOF:
                if i + 7 > n:
                    return None
                return int.from_bytes(data[i + 5:i + 7], "big"), int.from_bytes(data[i + 3:i + 5], "big")
            i += length
    return None


def decode_upload(data: bytes) -> np.ndarray:
    """Returns the decoded BGR image, or raises ScanError('bad_upload', reason)."""
    if len(data) < MIN_UPLOAD_BYTES:
        raise ScanError("bad_upload", f"file too small: {len(data)} bytes, first bytes {data[:16]!r}")
    kind = sniff(data)
    if kind is None:
        raise ScanError("bad_upload", f"not a JPEG or PNG: first bytes {data[:16]!r}")
    size = image_dimensions(data)
    if size is None or size[0] <= 0 or size[1] <= 0:
        raise ScanError("bad_upload", f"{kind} signature but the picture size cannot be read from its header")
    if size[0] * size[1] > MAX_IMAGE_PIXELS:
        raise ScanError("image_too_large", f"{size[0]}x{size[1]} = {size[0] * size[1]} pixels, limit {MAX_IMAGE_PIXELS}")
    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    if img is None or img.size == 0:
        raise ScanError("bad_upload", f"{kind} signature but it does not decode ({len(data)} bytes)")
    return img
