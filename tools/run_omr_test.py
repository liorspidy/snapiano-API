"""
OMR test bench: put sheet-music photos/scans in ./pages, run this script,
open results/report.html. Optional: put a correct MusicXML of the same page
in ./reference with the same file name to get an accuracy score.

Usage:
    python run_omr_test.py                      # homr only
    python run_omr_test.py --engines homr,oemer
    python run_omr_test.py --audiveris "C:/Program Files/Audiveris/Audiveris.exe" --engines homr,audiveris
"""
from __future__ import annotations

import argparse
import csv
import html
import json
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from snapiano_be.score_to_json import analyze, compare_to_reference, to_app_json  # noqa: E402

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}
INPUT_EXT = IMAGE_EXT | {".pdf"}
SCORE_EXT = {".musicxml", ".mxl", ".xml"}


# ---------- photo quality (the same checks will run on the phone before upload) ----------

def _count_staves(gray: np.ndarray) -> tuple[int, int]:
    """Finds long horizontal lines and groups them into 5-line staves."""
    bw = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV, 25, 15)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (max(20, gray.shape[1] // 20), 1))
    horiz = cv2.morphologyEx(bw, cv2.MORPH_OPEN, kernel)
    row_hits = (horiz.sum(axis=1) / 255) > gray.shape[1] * 0.15

    centers, start = [], None
    for y, hit in enumerate(row_hits):
        if hit and start is None:
            start = y
        elif not hit and start is not None:
            centers.append((start + y - 1) / 2)
            start = None
    if len(centers) < 4:
        return len(centers), 0

    gaps = np.diff(centers)
    spacing = float(np.median(np.sort(gaps)[: max(1, len(gaps) // 2)]))
    staves, run = 0, 1
    for g in gaps:
        if g < spacing * 1.8:
            run += 1
        else:
            staves += run >= 4
            run = 1
    staves += run >= 4
    return len(centers), staves


def image_quality(path: Path) -> dict:
    if path.suffix.lower() not in IMAGE_EXT:
        return {"verdict": "skipped", "reasons": ["PDF – לא נבדק"], "staves": None}
    img = cv2.imread(str(path))
    if img is None:
        return {"verdict": "unreadable", "reasons": ["הקובץ לא נפתח"], "staves": None}

    h, w = img.shape[:2]
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    scale = 1600 / max(h, w)
    if scale < 1:
        gray = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)

    sharpness = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    brightness = float(gray.mean())
    contrast = float(gray.std())
    lines, staves = _count_staves(gray)

    # Starting thresholds — calibrate them against your own results.
    reasons = []
    if min(h, w) < 1000:
        reasons.append("רזולוציה נמוכה")
    if sharpness < 80:
        reasons.append("מטושטש")
    if brightness < 90:
        reasons.append("חשוך מדי")
    elif brightness > 245:
        reasons.append("בהיר מדי")
    if contrast < 35:
        reasons.append("ניגודיות נמוכה")
    if staves == 0:
        reasons.append("לא זוהו חמשות (עקום או רחוק מדי?)")

    if not reasons:
        verdict = "good"
    elif staves == 0 or len(reasons) >= 2:
        verdict = "poor"
    else:
        verdict = "fair"
    return {
        "verdict": verdict, "reasons": reasons, "width": w, "height": h,
        "sharpness": round(sharpness), "brightness": round(brightness),
        "contrast": round(contrast), "staff_lines": lines, "staves": staves,
    }


# ---------- engines ----------

def _resolve_homr(user_cmd: str | None) -> list[str] | None:
    if user_cmd:
        return shlex.split(user_cmd)
    if shutil.which("homr"):
        return ["homr"]
    if shutil.which("uvx"):
        return ["uvx", "--from", "homr[cpu]", "homr"]
    return None


def build_command(engine: str, image: Path, workdir: Path, args) -> list[str] | None:
    if engine == "homr":
        base = _resolve_homr(args.homr)
        return base + [str(image)] if base else None  # writes next to the image
    if engine == "oemer":
        base = shlex.split(args.oemer) if args.oemer else (["oemer"] if shutil.which("oemer") else None)
        return base + [str(image), "-o", str(workdir)] if base else None
    if engine == "audiveris":
        if not args.audiveris:
            return None
        return shlex.split(args.audiveris) + ["-batch", "-export", "-output", str(workdir), str(image)]
    raise ValueError(engine)


def find_score(workdir: Path, started: float) -> Path | None:
    found = [p for p in workdir.rglob("*") if p.suffix.lower() in SCORE_EXT and p.stat().st_mtime >= started - 1]
    return max(found, key=lambda p: p.stat().st_mtime) if found else None


def run_engine(engine: str, page: Path, out_root: Path, args) -> dict:
    workdir = (out_root / engine / page.stem).resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    image = workdir / page.name
    shutil.copy2(page, image)

    cmd = build_command(engine, image, workdir, args)
    if cmd is None:
        return {"engine": engine, "ok": False, "error": f"{engine} not installed / not configured"}

    started = time.time()
    try:
        proc = subprocess.run(cmd, cwd=workdir, capture_output=True, text=True, timeout=args.timeout)
    except subprocess.TimeoutExpired:
        return {"engine": engine, "ok": False, "error": f"timeout after {args.timeout}s"}
    seconds = round(time.time() - started, 1)
    (workdir / "engine.log").write_text(proc.stdout + "\n" + proc.stderr, encoding="utf-8")

    score_path = find_score(workdir, started)
    if score_path is None:
        return {"engine": engine, "ok": False, "seconds": seconds,
                "error": f"no MusicXML produced (exit {proc.returncode}), see engine.log"}
    return {"engine": engine, "ok": True, "seconds": seconds, "score": score_path}


def evaluate(result: dict, page: Path, ref_dir: Path) -> dict:
    try:
        summary, events, score = analyze(result["score"])
    except Exception as exc:  # broken MusicXML is itself a result worth recording
        result.update(ok=False, error=f"MusicXML could not be parsed: {exc}")
        return result

    out_dir = result["score"].parent
    (out_dir / f"{page.stem}.json").write_text(
        json.dumps(to_app_json(summary, events), ensure_ascii=False, indent=1), encoding="utf-8")
    try:
        score.write("midi", fp=str(out_dir / f"{page.stem}.mid"))
        result["midi"] = out_dir / f"{page.stem}.mid"
    except Exception:
        pass

    result.update(
        measures=summary.measures, notes_right=summary.notes_right, notes_left=summary.notes_left,
        time_signature=summary.time_signature, key=summary.key, bpm=summary.bpm,
        measures_ok_pct=summary.measures_ok_pct,
        flagged=sorted({i.measure for i in summary.issues}),
    )

    ref = next((p for p in ref_dir.glob(f"{page.stem}.*") if p.suffix.lower() in SCORE_EXT), None)
    if ref:
        _, ref_events, _ = analyze(ref)
        result["reference"] = compare_to_reference(events, ref_events)
    return result


# ---------- report ----------

VERDICT_HE = {"good": "טוב", "fair": "בינוני", "poor": "חלש", "skipped": "—", "unreadable": "שגיאה"}


def write_reports(rows: list[dict], out_root: Path) -> None:
    with open(out_root / "report.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["page", "photo", "photo_issues", "staves", "engine", "ok", "seconds", "measures",
                    "measures_ok_pct", "flagged_measures", "notes_R", "notes_L", "time_sig", "key",
                    "ref_R_pitch", "ref_R_rhythm", "ref_L_pitch", "ref_L_rhythm", "error"])
        for r in rows:
            q, ref = r["quality"], r.get("reference", {})
            w.writerow([r["page"], q["verdict"], "; ".join(q["reasons"]), q.get("staves"), r["engine"],
                        r["ok"], r.get("seconds"), r.get("measures"), r.get("measures_ok_pct"),
                        " ".join(map(str, r.get("flagged", []))), r.get("notes_right"), r.get("notes_left"),
                        r.get("time_signature"), r.get("key"),
                        ref.get("R", {}).get("pitch_pct"), ref.get("R", {}).get("pitch_rhythm_pct"),
                        ref.get("L", {}).get("pitch_pct"), ref.get("L", {}).get("pitch_rhythm_pct"),
                        r.get("error", "")])

    def cell(v):
        return html.escape("" if v is None else str(v))

    body = []
    for r in rows:
        q, ref = r["quality"], r.get("reference", {})
        refs = " · ".join(f"{'ימין' if h == 'R' else 'שמאל'}: {v['pitch_pct']}% / {v['pitch_rhythm_pct']}%"
                          for h, v in ref.items()) or "—"
        links = ""
        if r.get("ok"):
            rel = lambda p: html.escape(str(Path(p).relative_to(out_root)).replace("\\", "/"))
            links = f'<a href="{rel(r["score"])}">MusicXML</a>'
            if r.get("midi"):
                links += f' · <a href="{rel(r["midi"])}">MIDI</a>'
        status = (f'{cell(r.get("measures_ok_pct"))}%' if r.get("ok")
                  else f'<span class="err">{cell(r.get("error"))}</span>')
        body.append(
            f"<tr><td>{cell(r['page'])}</td>"
            f"<td class='q-{q['verdict']}'>{VERDICT_HE.get(q['verdict'], q['verdict'])}"
            f"<small>{cell(', '.join(q['reasons']))}</small></td>"
            f"<td>{cell(r['engine'])}</td><td>{cell(r.get('seconds'))}</td>"
            f"<td>{cell(r.get('measures'))}</td><td>{status}</td>"
            f"<td>{cell(' '.join(map(str, r.get('flagged', []))) or '—')}</td>"
            f"<td>{cell(r.get('notes_right'))} / {cell(r.get('notes_left'))}</td>"
            f"<td>{cell(r.get('time_signature'))} {cell(r.get('key'))}</td>"
            f"<td>{refs}</td><td>{links}</td></tr>")

    page = f"""<!doctype html><html lang="he" dir="rtl"><meta charset="utf-8">
<title>דוח בדיקת זיהוי תווים</title>
<style>
body{{font-family:system-ui,sans-serif;margin:24px;color:#1b2230}}
table{{border-collapse:collapse;width:100%;font-size:14px}}
th,td{{border-bottom:1px solid #d8dde5;padding:8px;text-align:right;vertical-align:top}}
th{{background:#eef0f3}} small{{display:block;color:#5a6474}}
.q-good{{color:#1d6b3a}} .q-fair{{color:#8a5a00}} .q-poor,.err{{color:#b3261e}}
</style>
<h1>דוח בדיקת זיהוי תווים</h1>
<p>"תיבות תקינות" = אחוז התיבות שסכום המשכים בהן מתאים למשקל. דיוק מול מקור = התאמת רצף הצלילים
(גובה / גובה+משך) לקובץ MusicXML נכון, אם שמת כזה בתיקיית reference. הכי חשוב: לפתוח את קובץ ה-MIDI ולשמוע.</p>
<table><tr><th>דף</th><th>איכות צילום</th><th>מנוע</th><th>שניות</th><th>תיבות</th><th>תיבות תקינות</th>
<th>תיבות לבדיקה</th><th>תווים ימין / שמאל</th><th>משקל וסולם</th><th>דיוק מול מקור</th><th>קבצים</th></tr>
{''.join(body)}</table></html>"""
    (out_root / "report.html").write_text(page, encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser(description="Test OMR engines on your own sheet-music pages")
    ap.add_argument("--pages", default="pages")
    ap.add_argument("--reference", default="reference")
    ap.add_argument("--out", default="results")
    ap.add_argument("--engines", default="homr", help="comma list: homr,oemer,audiveris")
    ap.add_argument("--homr", help='custom homr command, e.g. "uvx --from homr[cpu] homr"')
    ap.add_argument("--oemer", help="custom oemer command")
    ap.add_argument("--audiveris", help="path to the Audiveris executable")
    ap.add_argument("--timeout", type=int, default=900, help="seconds per page per engine")
    args = ap.parse_args()

    pages_dir, ref_dir, out_root = Path(args.pages), Path(args.reference), Path(args.out).resolve()
    pages = sorted(p for p in pages_dir.glob("*") if p.suffix.lower() in INPUT_EXT)
    if not pages:
        sys.exit(f"No images found in {pages_dir.resolve()} — add .jpg/.png/.pdf files and run again.")
    out_root.mkdir(parents=True, exist_ok=True)
    engines = [e.strip() for e in args.engines.split(",") if e.strip()]

    rows = []
    for page in pages:
        quality = image_quality(page)
        print(f"\n{page.name}: photo={quality['verdict']} staves={quality.get('staves')}")
        for engine in engines:
            print(f"  {engine} ...", end="", flush=True)
            result = run_engine(engine, page, out_root, args)
            if result["ok"]:
                result = evaluate(result, page, ref_dir)
            result.update(page=page.name, quality=quality)
            rows.append(result)
            if result["ok"]:
                print(f" {result['seconds']}s, {result['measures']} measures, "
                      f"{result['measures_ok_pct']}% ok, flagged: {result['flagged'] or '-'}")
            else:
                print(f" FAILED: {result['error']}")

    write_reports(rows, out_root)
    print("\nSummary per engine:")
    for engine in engines:
        ok = [r for r in rows if r["engine"] == engine and r["ok"]]
        avg = round(sum(r["measures_ok_pct"] for r in ok) / len(ok), 1) if ok else 0
        avg_s = round(sum(r["seconds"] for r in ok) / len(ok), 1) if ok else 0
        print(f"  {engine}: {len(ok)}/{len(pages)} pages read, avg measures ok {avg}%, avg {avg_s}s/page")
    print(f"\nOpen {(out_root / 'report.html').resolve()}")


if __name__ == "__main__":
    main()
