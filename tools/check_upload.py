"""Read-only: shows whether uploaded scan files are real, decodable images.

    python tools/check_upload.py              # the 10 newest scans
    python tools/check_upload.py -n 3         # the 3 newest
    python tools/check_upload.py --path <user_id>/<file>.jpg ...   # specific storage objects (bucket 'scans')

Needs SUPABASE_URL and SUPABASE_SERVICE_KEY (read from BE/.env). For each file: stored size and MIME type, first bytes,
what the signature says, whether OpenCV (the same decode the worker uses) opens it, and the scan's status/error code.
A file that uploads and decodes is NOT proof that homr reads it correctly: that is a separate result (the scan status).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from snapiano_be.upload_guard import MIN_UPLOAD_BYTES, ScanError, decode_upload, sniff  # noqa: E402
from snapiano_be.worker import db  # noqa: E402  (importing it loads BE/.env)


def stored_meta(path: str) -> dict:
    folder, name = path.rsplit("/", 1)
    for o in db.storage.from_("scans").list(folder, {"limit": 1000}):
        if o["name"] == name:
            return o.get("metadata") or {}
    return {}


def report(path: str, scan: dict | None = None) -> None:
    meta = stored_meta(path)
    data = db.storage.from_("scans").download(path)
    kind = sniff(data)
    try:
        h, w = decode_upload(data).shape[:2]
        decode = f"decodes {w}x{h}"
    except ScanError as e:
        decode = f"DOES NOT DECODE ({e.detail})"
    line = (f"{path.split('/')[-1]}: {len(data)} bytes (stored size {meta.get('size')}, stored type {meta.get('mimetype')}) "
            f"| first bytes {data[:8].hex(' ')} | signature {kind} | {decode}")
    if scan:
        line += f" | scan {scan['status']}" + (f", error code {scan['error']}" if scan.get("error") else "")
    print(line)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", type=int, default=10)
    ap.add_argument("--path", nargs="*")
    args = ap.parse_args()
    print(f"guard: files under {MIN_UPLOAD_BYTES} bytes are rejected before decoding")
    if args.path:
        for p in args.path:
            report(p)
        return
    for s in db.table("scans").select("image_path,status,error,created_at").order("created_at", desc=True).limit(args.n).execute().data:
        report(s["image_path"], s)


if __name__ == "__main__":
    main()
