"""
Review-screen prototype: for every measure that doesn't add up, show
  1. the measure cut out of the original photo (homr writes each note's position),
  2. the measure as the system read it,
  3. up to 3 alternative readings that make the measure add up, most likely first.
The user only has to say which picture looks like the photo. No music reading needed.

    python -m snapiano_be.review pages/page1.jpg results/homr/page1/page1.musicxml

Note: per-note positions (imgpos) need homr from GitHub main:
    pip install "homr[cpu] @ git+https://github.com/liebharc/homr"
"""
from __future__ import annotations

import base64
import copy
import re
import sys
import tempfile
from pathlib import Path

import cv2
import verovio
from music21 import converter, note

from .score_to_json import EPS, _hand_for_part, analyze


# ---------- where each measure is on the photo ----------

def measure_boxes(musicxml_text: str) -> dict[int, tuple[int, int, int, int]]:
    rows, system = [], 0
    for num, body in re.findall(r'<measure number="(\d+)"[^>]*>(.*?)</measure>', musicxml_text, re.S):
        if 'new-system="yes"' in body and rows:
            system += 1
        pts = [(int(x), int(y)) for x, y in re.findall(r"imgpos: (\d+), (\d+)", body)]
        rows.append((int(num), system, pts))

    sys_y = {}
    for _, s, pts in rows:
        ys = [y for _, y in pts]
        if ys:
            lo, hi = sys_y.get(s, (10**9, -1))
            sys_y[s] = (min(lo, min(ys)), max(hi, max(ys)))

    boxes = {}
    for i, (num, s, pts) in enumerate(rows):
        if not pts:
            continue
        xs = [x for x, _ in pts]
        nxt = rows[i + 1] if i + 1 < len(rows) else None
        right = min(x for x, _ in nxt[2]) - 12 if nxt and nxt[1] == s and nxt[2] else max(xs) + 70
        y0, y1 = sys_y[s]
        boxes[num] = (min(xs) - 45, y0 - 110, right, y1 + 90)
    return boxes


def crop_png(image_path: Path, box) -> str:
    img = cv2.imread(str(image_path))
    h, w = img.shape[:2]
    x0, y0, x1, y1 = max(0, box[0]), max(0, box[1]), min(w, box[2]), min(h, box[3])
    ok, buf = cv2.imencode(".png", img[y0:y1, x0:x1])
    return "data:image/png;base64," + base64.b64encode(buf).decode()


# ---------- alternative readings ----------

def _repack(voice) -> None:
    offset = 0.0
    for el in sorted(voice.notesAndRests, key=lambda e: e.offset):
        el.offset = offset
        offset += el.quarterLength


def _valid(el) -> bool:
    return el.duration.type != "complex"


def alternatives(score, part_index: int, measure_no: int, expected: float) -> list[tuple[str, float, object]]:
    """Heuristic candidates, most likely first. Later: replace the priors with the model's own probabilities."""
    base_part = score.parts[part_index]
    base_m = base_part.measure(measure_no)
    voices = list(base_m.voices) or [base_m]
    vi = max(range(len(voices)), key=lambda i: abs(_measure_content_voice(voices[i]) - expected))
    found = _measure_content_voice(voices[vi])
    gap = expected - found

    out, seen = [], set()

    def attempt(label: str, prior: float, change) -> None:
        sc = copy.deepcopy(score)
        m = sc.parts[part_index].measure(measure_no)
        v = (list(m.voices) or [m])[vi]
        if change(v):
            _repack(v)
            sig = tuple((float(e.quarterLength), tuple(p.midi for p in getattr(e, "pitches", ())))
                        for e in items(v))
            if abs(_measure_content_voice(v) - expected) < EPS and sig not in seen:
                seen.add(sig)  # two different fixes can produce the same measure; show it once
                out.append((label, prior, sc))

    def items(v):
        return sorted(v.notesAndRests, key=lambda e: e.offset)

    if gap > 0:
        for k, el in enumerate(items(voices[vi])):
            if el.duration.dots == 0 and abs(el.quarterLength * 0.5 - gap) < EPS:
                attempt("נקודה שלא זוהתה ליד תו", 0.5,
                        lambda v, k=k: (setattr(items(v)[k].duration, "dots", 1), True)[1])
        def lengthen(v):
            last = items(v)[-1]
            last.quarterLength = last.quarterLength + gap
            return _valid(last)
        attempt("התו האחרון ארוך יותר", 0.3, lengthen)
        def add_rest(v):
            r = note.Rest(quarterLength=gap)
            if not _valid(r):
                return False
            v.insert(found, r)
            return True
        attempt("שתיקה שלא זוהתה בסוף", 0.2, add_rest)
    elif gap < 0:
        extra = -gap
        for k, el in enumerate(items(voices[vi])):
            if el.duration.dots == 1 and abs(el.quarterLength / 3 - extra) < EPS:
                attempt("נקודה שזוהתה בטעות", 0.5,
                        lambda v, k=k: (setattr(items(v)[k].duration, "dots", 0), True)[1])
        def shorten(v):
            last = items(v)[-1]
            if last.quarterLength - extra <= 0:
                return False
            last.quarterLength = last.quarterLength - extra
            return _valid(last)
        attempt("התו האחרון קצר יותר", 0.3, shorten)
        def drop_last(v):
            last = items(v)[-1]
            if abs(last.quarterLength - extra) > EPS:
                return False
            v.remove(last)
            return True
        attempt("תו מיותר בסוף", 0.2, drop_last)

    out.sort(key=lambda c: -c[1])
    return out[:3]


def _measure_content_voice(v) -> float:
    return sum(n.quarterLength for n in v.notesAndRests)


# ---------- rendering ----------

class Renderer:
    def __init__(self) -> None:
        self.tk = verovio.toolkit()
        self.tk.setOptions({"pageWidth": 900, "pageHeight": 3000, "adjustPageHeight": True, "scale": 45,
                            "header": "none", "footer": "none", "svgViewBox": True, "svgRemoveXlink": True})

    def measure(self, musicxml_path: Path, n: int) -> str:
        text = musicxml_path.read_text(encoding="utf-8")
        text = re.sub(r"<part-name>[^<]*</part-name>", "<part-name></part-name>", text)  # no "Piano" label
        self.tk.loadData(text)
        self.tk.select({"measureRange": f"{n}-{n}"})
        self.tk.redoLayout()
        return self.tk.renderToSVG(1)

    def measure_of(self, score, n: int) -> str:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "alt.musicxml"
            score.write("musicxml", fp=str(p))
            return self.measure(p, n)


PAGE = """<!doctype html><html lang="he" dir="rtl"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>בדיקת תיבות</title>
<style>
body{margin:0;padding:16px;background:#EDEFF3;color:#1A2233;font-family:system-ui,sans-serif}
section{background:#fff;border-radius:14px;padding:16px;margin-bottom:16px;max-width:760px}
h2{margin:0 0 12px;font-size:20px} h3{margin:16px 0 6px;font-size:15px;color:#3A4354}
img,svg{max-width:100%;height:auto;display:block;background:#fff}
.photo{border:2px solid #1A2233;border-radius:8px}
.alt{border:1px solid #C9CFD9;border-radius:8px;padding:6px;margin-bottom:8px}
.alt.best{border:2px solid #1A2233}
</style>
<h1 style="font-size:22px">בדיקת תיבות: מה מהאפשרויות נראה כמו בתמונה?</h1>
__SECTIONS__
</html>"""


def main() -> None:
    if len(sys.argv) < 3:
        raise SystemExit("usage: python prototypes/review_measures.py <photo> <file.musicxml> [out.html]")
    photo, xml = Path(sys.argv[1]), Path(sys.argv[2])
    out = Path(sys.argv[3]) if len(sys.argv) > 3 else xml.with_name("review_demo.html")

    summary, _, score = analyze(xml)
    boxes = measure_boxes(xml.read_text(encoding="utf-8"))
    parts = list(score.parts)
    hands = [_hand_for_part(p, i, len(parts)) for i, p in enumerate(parts)]
    r = Renderer()

    sections = []
    for issue in summary.issues:
        pi = hands.index(issue.hand) if issue.hand in hands else 0
        html = [f"<section><h2>תיבה {issue.measure}</h2>"]
        if issue.measure in boxes:
            html.append(f'<h3>בתמונה</h3><img class="photo" alt="תיבה {issue.measure} מתוך הצילום" '
                        f'src="{crop_png(photo, boxes[issue.measure])}">')
        html.append("<h3>מה שהמערכת קראה</h3>" + r.measure(xml, issue.measure))
        for i, (label, _, sc) in enumerate(alternatives(score, pi, issue.measure, issue.expected)):
            tag = " (הכי סביר, נבחר אוטומטית)" if i == 0 else ""
            html.append(f'<div class="alt{" best" if i == 0 else ""}"><h3>אפשרות {i + 1}{tag}: {label}</h3>'
                        f"{r.measure_of(sc, issue.measure)}</div>")
        html.append("</section>")
        sections.append("".join(html))
        print(f"measure {issue.measure} ({issue.hand}): expected {issue.expected}, found {issue.found}")

    out.write_text(PAGE.replace("__SECTIONS__", "".join(sections) or "<p>אין תיבות לבדיקה.</p>"), encoding="utf-8")
    print(f"Open {out.resolve()}")


if __name__ == "__main__":
    main()
