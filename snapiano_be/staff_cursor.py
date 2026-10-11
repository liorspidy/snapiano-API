"""
Staff + moving cursor prototype.

Renders a MusicXML file with Verovio (one tall page, automatic line breaks for
a phone held sideways) plus Verovio's timemap, then writes staff_demo.html:
it shows only the line of music being played, moves a vertical cursor exactly
to each note as it sounds, colors the sounding notes by hand (right = blue,
left = orange) and turns to the next line by itself. BPM can be changed live.

    python prototypes/staff_cursor.py results/homr/page1/page1.musicxml
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import verovio

PAGE_WIDTH = 1800  # Verovio units; roughly what fits a phone in landscape at a readable size


def render(musicxml: Path) -> tuple[str, list[dict]]:
    tk = verovio.toolkit()
    tk.setOptions({
        "pageWidth": PAGE_WIDTH,
        "pageHeight": 60000,
        "adjustPageHeight": True,
        "breaks": "auto",
        "scale": 40,
        "header": "none",
        "footer": "none",
        "svgViewBox": True,
        "svgRemoveXlink": True,
    })
    text = re.sub(r"<part-name>[^<]*</part-name>", "<part-name></part-name>", musicxml.read_text(encoding="utf-8"))
    if not tk.loadData(text):
        raise SystemExit(f"Verovio could not read {musicxml}")
    svg = tk.renderToSVG(1)
    timemap = tk.renderToTimemap({"includeMeasures": True, "includeRests": False})
    return svg, timemap


def onsets(timemap: list[dict]) -> list[dict]:
    """Only the moments where notes start, with their quarter-note time (independent of tempo)."""
    return [{"q": e["qstamp"], "on": e["on"]} for e in timemap if e.get("on")]


HTML = """<!doctype html><html lang="he" dir="rtl"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>חמשה עם סמן נגינה</title>
<style>
:root{--ink:#1A2233;--rh:#2F5FE8;--lh:#E08A1E;--paper:#FFFFFF;--ground:#EDEFF3}
body{margin:0;font-family:system-ui,sans-serif;background:var(--ground);color:var(--ink)}
.bar{display:flex;gap:8px;align-items:center;padding:12px 16px;flex-wrap:wrap}
button{min-width:48px;height:48px;border-radius:24px;border:1px solid #A9B1BE;background:#fff;font-size:18px;cursor:pointer}
button.primary{background:var(--ink);color:#fff;border:0;padding:0 20px}
#bpm{font-weight:700;min-width:3ch;text-align:center}
.sheet{margin:0 16px;background:var(--paper);border-radius:12px;padding:8px 0;overflow:hidden}
.sheet > svg{width:100%;height:auto;display:block}
.rh, .rh *{fill:var(--rh)!important;stroke:var(--rh)!important}
.lh, .lh *{fill:var(--lh)!important;stroke:var(--lh)!important}
#line{font-size:14px;color:#566072;margin-inline-start:auto}
</style>
<div class="bar">
  <button class="primary" id="play">ניגון</button>
  <button id="slower" aria-label="להאט">−</button><span id="bpm"></span><button id="faster" aria-label="להאיץ">+</button>
  <span id="line"></span>
</div>
<div class="sheet" dir="ltr">__SVG__</div>
<script>
const ONSETS = __ONSETS__;
let bpm = 72, playing = false, q = 0, last = null, idx = 0;
const svg = document.querySelector('.sheet svg');
const inner = svg.querySelector('svg.definition-scale') || svg;
const systems = [...inner.querySelectorAll('g.system')];
const sysBox = systems.map(s => s.getBBox());
const noteSystem = {}, noteHand = {}, noteX = {};
systems.forEach((s, i) => s.querySelectorAll('g.note').forEach(n => {
  noteSystem[n.id] = i;
  const staff = n.closest('g.staff');
  const staves = [...staff.parentNode.querySelectorAll(':scope > g.staff')];
  noteHand[n.id] = staves.indexOf(staff) === 0 ? 'rh' : 'lh';
  const head = n.querySelector('.notehead') || n;
  const b = head.getBBox(); noteX[n.id] = b.x + b.width / 2;
}));
const steps = ONSETS.map(o => {
  const ids = o.on.filter(id => id in noteSystem);
  if (!ids.length) return null;
  return {q: o.q, ids, sys: noteSystem[ids[0]], x: ids.reduce((a, id) => a + noteX[id], 0) / ids.length};
}).filter(Boolean);

const NS = 'http://www.w3.org/2000/svg';
const cursor = document.createElementNS(NS, 'line');
cursor.setAttribute('stroke', '#1A2233'); cursor.setAttribute('stroke-width', '45');
cursor.setAttribute('stroke-linecap', 'round');
inner.appendChild(cursor);
let shownSys = -1, lit = [];

function show(sys) {
  if (sys === shownSys) return;
  shownSys = sys;
  const b = sysBox[sys], pad = 250;
  const vb = inner.getAttribute('viewBox').split(' ').map(Number);
  // crop the tall page to the current line only
  inner.setAttribute('viewBox', [vb[0], b.y - pad, vb[2], b.height + pad * 2].join(' '));
  svg.setAttribute('viewBox', [0, 0, 1000, 1000 * (b.height + pad * 2) / vb[2]].join(' '));
  cursor.setAttribute('y1', b.y - 120); cursor.setAttribute('y2', b.y + b.height + 120);
  document.getElementById('line').textContent = 'שורה ' + (sys + 1) + ' מתוך ' + systems.length;
}

function draw() {
  while (idx < steps.length - 1 && steps[idx + 1].q <= q) idx++;
  const s = steps[idx], n = steps[idx + 1];
  show(s.sys);
  let x = s.x;
  if (n && n.sys === s.sys && n.q > s.q) x = s.x + (n.x - s.x) * Math.min(1, (q - s.q) / (n.q - s.q));
  cursor.setAttribute('x1', x); cursor.setAttribute('x2', x);
  lit.forEach(id => document.getElementById(id)?.classList.remove('rh', 'lh'));
  lit = s.ids; lit.forEach(id => document.getElementById(id)?.classList.add(noteHand[id]));
}

function tick(t) {
  if (!playing) return;
  if (last !== null) q += (t - last) / 60000 * bpm;
  last = t;
  if (q > steps[steps.length - 1].q + 2) { q = 0; idx = 0; }
  draw(); requestAnimationFrame(tick);
}
const bpmEl = document.getElementById('bpm'); bpmEl.textContent = bpm;
document.getElementById('play').onclick = e => {
  playing = !playing; last = null; e.target.textContent = playing ? 'השהיה' : 'ניגון';
  if (playing) requestAnimationFrame(tick);
};
document.getElementById('slower').onclick = () => { bpm = Math.max(30, bpm - 6); bpmEl.textContent = bpm; };
document.getElementById('faster').onclick = () => { bpm = Math.min(200, bpm + 6); bpmEl.textContent = bpm; };
draw();
</script></html>"""


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("usage: python prototypes/staff_cursor.py <file.musicxml> [out.html]")
    src = Path(sys.argv[1])
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else src.with_name("staff_demo.html")
    svg, timemap = render(src)
    page = HTML.replace("__SVG__", svg).replace("__ONSETS__", json.dumps(onsets(timemap)))
    out.write_text(page, encoding="utf-8")
    print(f"Open {out.resolve()}")


if __name__ == "__main__":
    main()
