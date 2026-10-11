"""Polls Supabase for queued scans and turns each photo into a piece.

    SUPABASE_URL=... SUPABASE_SERVICE_KEY=... python -m snapiano_be.worker

Per scan: download photo -> cleaning.clean -> homr on the CLEAN page -> timeline + bar checks ->
line images to storage 'pieces/<user>/<piece>/lineNN.png' -> pieces row (notes_json) -> scan done.
The service key bypasses RLS: keep it on the server only.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import time
import traceback
from pathlib import Path

import cv2
import numpy as np
from supabase import create_client

from .bundle import build_lines
from .cleaning import clean
from .score_to_json import analyze, to_app_json
from .timeline import read_timeline

POLL_SECONDS = 3

# Read BE/.env without a shell, so special characters in values can't break anything.
for line in (Path(__file__).resolve().parent.parent / ".env").read_text().splitlines():
    if "=" in line and not line.startswith("#"):
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

db = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])


def claim_next() -> dict | None:
    """Oldest queued scan, claimed atomically (the status filter makes a second worker lose the race)."""
    rows = db.table("scans").select("*").eq("status", "queued").order("created_at").limit(1).execute().data
    if not rows:
        return None
    claimed = db.table("scans").update({"status": "processing"}).eq("id", rows[0]["id"]).eq("status", "queued").execute().data
    return claimed[0] if claimed else None


def png_bytes(img: np.ndarray) -> bytes:
    return cv2.imencode(".png", img)[1].tobytes()


def process(scan: dict) -> None:
    photo = cv2.imdecode(np.frombuffer(db.storage.from_("scans").download(scan["image_path"]), np.uint8), cv2.IMREAD_COLOR)
    if photo is None:
        raise ValueError("unreadable image")
    page, info = clean(photo)
    if not info.systems:
        raise ValueError("no staves found")

    with tempfile.TemporaryDirectory() as tmp:
        clean_png = Path(tmp) / "page.png"
        cv2.imwrite(str(clean_png), page)
        subprocess.run(["homr", str(clean_png)], check=True, capture_output=True, timeout=600)  # AGPL: separate process
        xml = clean_png.with_suffix(".musicxml")
        notes = read_timeline(str(xml))
        summary, events, _ = analyze(xml)
    if not notes:
        raise ValueError("no notes read")

    images, marks = build_lines(page, info.systems, notes)
    app = to_app_json(summary, events)
    app["notes"] = marks  # the app needs positions on the line images, not the plain events

    piece_id = db.table("pieces").insert({"user_id": scan["user_id"]}).execute().data[0]["id"]
    app["lines"] = []
    for i, img in enumerate(images, 1):
        path = f"{scan['user_id']}/{piece_id}/line{i:02d}.png"
        db.storage.from_("pieces").upload(path, png_bytes(img), {"content-type": "image/png"})
        app["lines"].append({"path": path, "w": img.shape[1], "h": img.shape[0]})
    db.table("pieces").update({"notes_json": app}).eq("id", piece_id).execute()
    db.table("scans").update({
        "status": "done", "piece_id": piece_id,
        "measures": summary.measures, "flagged_measures": app["flaggedMeasures"],
    }).eq("id", scan["id"]).execute()


def main() -> None:
    print("worker running")
    while True:
        scan = claim_next()
        if not scan:
            time.sleep(POLL_SECONDS)
            continue
        try:
            process(scan)
        except Exception as e:  # any failure -> the app shows "couldn't read this page"
            traceback.print_exc()
            db.table("scans").update({"status": "failed", "error": str(e)[:500]}).eq("id", scan["id"]).execute()


if __name__ == "__main__":
    main()
