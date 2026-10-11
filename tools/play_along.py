"""
Builds play_along.html: the real cleaned scan, one line at a time, with a cursor that sits on the
printed notes, colored marks on the notes that sound (blue = right hand, orange = left hand), a
keyboard, and two modes:
  - האזנה: plays the piece at the chosen BPM
  - המתנה: waits until you play the right notes (MIDI piano, microphone, or tap the keys)

Pipeline it demonstrates: clean_page.py -> homr on the CLEAN page (so note positions are in the
clean image) -> timeline.py -> this page.

    python cleaning/clean_page.py pages/bach.jpg out/
    homr out/bach_clean.png
    python prototypes/play_along.py out/bach_clean.png out/bach_clean.json out/bach_clean.musicxml
Open play_along.html in Chrome (MIDI and microphone need Chrome or Edge).
"""
from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

import cv2

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from snapiano_be.cleaning import find_staves, snap_to_noteheads, straighten_band_with_shift  # noqa: E402
from snapiano_be.timeline import read_timeline  # noqa: E402

LISTEN_JS = HERE.parent.parent / "FE" / "src" / "audio" / "listen.js"


def build(page_png: Path, clean_json: Path, musicxml: Path, out: Path) -> None:
    page = cv2.imread(str(page_png), cv2.IMREAD_GRAYSCALE)
    systems = json.loads(clean_json.read_text())["systems"]
    notes = [n for n in read_timeline(str(musicxml)) if n.x is not None]

    lines, data = [], []
    for i, (y0, y1) in enumerate(systems):
        img, shift = straighten_band_with_shift(page[y0:y1])
        ok, buf = cv2.imencode(".png", img)
        lines.append({"src": "data:image/png;base64," + base64.b64encode(buf).decode(),
                      "w": img.shape[1], "h": img.shape[0]})
        here = [n for n in notes if y0 <= n.y < y1]
        staves, spacing = find_staves(img)
        exact = []
        for n in here:
            if len(staves) >= n.staff:  # y from the pitch itself: exact line or space on that staff
                bottom = staves[n.staff - 1][1]
                exact.append((n.x, bottom - (n.diatonic - n.bottom_line) * spacing / 2))
            else:
                exact.append((n.x, n.y - y0 + float(shift[min(max(n.x, 0), img.shape[1] - 1)])))
        for n, (x, y) in zip(here, snap_to_noteheads(img, exact, spacing)):
            data.append({"m": n.midi, "s": n.start, "d": n.dur, "h": n.hand, "line": i,
                         "x": round(float(x), 1), "y": round(float(y), 1)})
    data.sort(key=lambda d: (d["s"], d["m"]))
    listen_js = LISTEN_JS.read_text(encoding="utf-8")
    html = (TEMPLATE.replace("__LINES__", json.dumps(lines))
            .replace("__NOTES__", json.dumps(data))
            .replace("__LISTEN__", listen_js))
    out.write_text(html, encoding="utf-8")
    print(f"{len(data)} notes on {len(lines)} lines -> {out.resolve()}")


TEMPLATE = r"""<!doctype html><html lang="he" dir="rtl"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>נגינה עם הדף הסרוק</title>
<style>
:root{--ink:#1A2233;--muted:#566072;--rh:#2F5FE8;--lh:#F2A23A;--stage:#151B28;--raised:#232B3B}
*{box-sizing:border-box} body{margin:0;background:var(--stage);color:#fff;font-family:system-ui,sans-serif}
.bar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;padding:10px 12px}
button{min-height:48px;min-width:48px;padding:0 14px;border:0;border-radius:12px;background:var(--raised);color:#fff;font:inherit;cursor:pointer}
button[aria-pressed=true]{background:#fff;color:var(--stage)}
#status{flex:1 1 220px;min-height:48px;display:flex;align-items:center;padding:0 12px;border-radius:12px;background:#0E1320;border:1px solid #344057}
.sheet{position:relative;height:170px;margin:0 12px;background:#fff;border-radius:10px;overflow:hidden;direction:ltr}
.sheet img{position:absolute;top:0;left:0;height:100%;transition:left .12s linear}
.cursor{position:absolute;top:6px;bottom:6px;width:2px;margin-left:-1px;background:var(--ink)}
.mark{position:absolute;border-radius:50%;opacity:.45;pointer-events:none}
.keys{position:relative;height:110px;margin:10px 12px 12px;direction:ltr}
.key{position:absolute;top:0;border:1px solid #9AA3B2;border-top:0;border-radius:0 0 6px 6px;background:#fff;color:#5C6472;font-size:12px;display:flex;align-items:flex-end;justify-content:center;padding-bottom:6px}
.key.black{background:var(--ink);height:66px;z-index:2;border:0}
.lvl{width:60px;height:8px;border-radius:4px;background:var(--raised);overflow:hidden}.lvl i{display:block;height:100%;width:0;background:#7FE0A3}
</style>
<div class="bar">
  <button id="midi">חיבור פסנתר (MIDI)</button>
  <button id="mic">מיקרופון</button><span class="lvl" aria-hidden="true"><i id="lvl"></i></span>
  <button id="mWait" aria-pressed="true">המתנה</button><button id="mListen" aria-pressed="false">האזנה</button>
  <button id="slower" aria-label="להאט">−</button><span id="bpm"></span><button id="faster" aria-label="להאיץ">+</button>
  <button id="play">ניגון</button>
</div>
<div class="bar"><div id="status" role="status"></div><span id="where"></span></div>
<div class="sheet" id="sheet"><img id="img" alt="שורת התווים הנוכחית"><div class="cursor" id="cursor"></div></div>
<div class="keys" id="keys"></div>
<script>__LISTEN__</script>
<script>
const LINES = __LINES__, NOTES = __NOTES__;
const RH = '#2F5FE8', LH = '#F2A23A';
const SOLF = ['דו','דו#','רה','רה#','מי','פה','פה#','סול','סול#','לה','לה#','סי'];
const nm = m => SOLF[m % 12];
let bpm = 66, mode = 'wait', playing = false, t = NOTES.length ? NOTES[0].s : 0, last = null, pressed = new Set(), msg = '';
const onsets = [...new Set(NOTES.map(n => n.s))].sort((a, b) => a - b);
const sheet = document.getElementById('sheet'), img = document.getElementById('img'), cur = document.getElementById('cursor');

// keyboard covering the notes of the piece
const lo = Math.min(...NOTES.map(n => n.m)) - (Math.min(...NOTES.map(n => n.m)) % 12), hi = Math.max(...NOTES.map(n => n.m)) + 1;
const keysEl = document.getElementById('keys'); const keyEl = {};
const whites = []; for (let m = lo; m <= hi; m++) if (![1,3,6,8,10].includes(m % 12)) whites.push(m);
function layoutKeys() {
  const W = keysEl.clientWidth, ww = W / whites.length; keysEl.innerHTML = '';
  for (let m = lo; m <= hi; m++) {
    const b = [1,3,6,8,10].includes(m % 12), k = document.createElement('button');
    k.className = 'key' + (b ? ' black' : ''); k.setAttribute('aria-label', nm(m));
    const wi = whites.filter(x => x < m).length;
    k.style.left = (b ? wi * ww - ww * 0.29 : wi * ww) + 'px'; k.style.width = (b ? ww * 0.58 : ww) + 'px';
    if (!b) k.style.height = '110px';
    k.onclick = () => heard(m, 'tap'); keysEl.appendChild(k); keyEl[m] = k;
  }
}
layoutKeys(); addEventListener('resize', () => { layoutKeys(); draw(); });

// simple synth for listening mode (the app uses sampled piano)
let actx;
function tone(m, dur) {
  actx = actx || new AudioContext(); const o = actx.createOscillator(), g = actx.createGain();
  o.type = 'triangle'; o.frequency.value = 440 * Math.pow(2, (m - 69) / 12);
  g.gain.setValueAtTime(0.18, actx.currentTime); g.gain.exponentialRampToValueAtTime(0.001, actx.currentTime + Math.min(2.5, dur * 60 / bpm + 0.3));
  o.connect(g).connect(actx.destination); o.start(); o.stop(actx.currentTime + 2.6);
}

function sounding() { return mode === 'wait' ? NOTES.filter(n => n.s === t) : NOTES.filter(n => n.s <= t && t < n.s + n.d); }
function lineAt() { const n = NOTES.find(n => n.s >= t) || NOTES[NOTES.length - 1]; return n.line; }
function xAt(line) {
  const here = NOTES.filter(n => n.line === line);
  const at = s => { const g = here.filter(n => n.s === s); return g.reduce((a, n) => a + n.x, 0) / g.length; };
  const os = [...new Set(here.map(n => n.s))].sort((a, b) => a - b);
  let i = os.findIndex(s => s > t) - 1; if (i < 0) i = os[0] > t ? 0 : os.length - 1;
  const a = os[i], b = os[i + 1];
  if (b === undefined || t <= a) return at(a);
  return at(a) + (at(b) - at(a)) * (t - a) / (b - a);
}
let shownLine = -1;
function draw() {
  const line = lineAt(), L = LINES[line], k = sheet.clientHeight / L.h;
  if (line !== shownLine) { img.src = L.src; shownLine = line; }
  const x = xAt(line) * k, left = Math.min(0, Math.max(sheet.clientWidth - L.w * k, sheet.clientWidth * 0.3 - x));
  img.style.left = left + 'px'; cur.style.left = (left + x) + 'px';
  sheet.querySelectorAll('.mark').forEach(e => e.remove());
  const now = sounding();
  now.filter(n => n.line === line).forEach(n => {
    const d = document.createElement('div'); d.className = 'mark'; const r = 15;
    d.style.cssText = `left:${left + n.x * k - r}px;top:${n.y * k - r}px;width:${2*r}px;height:${2*r}px;background:${n.h === 'R' ? RH : LH}`;
    sheet.appendChild(d);
  });
  for (const m in keyEl) { const k2 = keyEl[m]; k2.style.background = ''; k2.textContent = ''; }
  now.forEach(n => { const k2 = keyEl[n.m]; if (k2) { k2.style.background = n.h === 'R' ? RH : LH; k2.textContent = nm(n.m); k2.style.color = n.h === 'R' ? '#fff' : '#1A2233'; } });
  const need = mode === 'wait' ? now.filter(n => !pressed.has(n.m)) : [];
  const words = h => need.filter(n => n.h === h).map(n => nm(n.m)).join(' ו');
  document.getElementById('status').textContent = msg || (mode === 'wait'
    ? 'מחכה לך: ' + [['R', 'ביד ימין'], ['L', 'ביד שמאל']].filter(([h]) => words(h)).map(([h, w]) => words(h) + ' ' + w).join(', ')
    : (playing ? 'מנגן בקצב ' + bpm : 'לחץ ניגון'));
  document.getElementById('where').textContent = 'שורה ' + (line + 1) + ' מתוך ' + LINES.length;
  listener.expect(need.map(n => n.m));
}

function heard(m, src) {
  if (mode !== 'wait') return;
  const need = NOTES.filter(n => n.s === t);
  if (need.some(n => n.m === m)) {
    pressed.add(m); msg = '';
    if (need.every(n => pressed.has(n.m))) { const i = onsets.indexOf(t); t = onsets[(i + 1) % onsets.length]; pressed.clear(); }
  } else msg = 'שמעתי ' + nm(m) + '. נגן את המקשים הצבועים';
  draw();
}
const listener = new PlayAlongListener({ onNote: heard, onLevel: v => document.getElementById('lvl').style.width = (v * 100) + '%' });

function tick(ts) {
  if (!playing || mode !== 'listen') return;
  if (last != null) {
    const nt = t + (ts - last) / 60000 * bpm;
    NOTES.filter(n => n.s > t && n.s <= nt).forEach(n => tone(n.m, n.d));
    t = nt > onsets[onsets.length - 1] + 2 ? onsets[0] : nt;
  }
  last = ts; draw(); requestAnimationFrame(tick);
}
const setMode = m => { mode = m; msg = ''; pressed.clear(); document.getElementById('mWait').setAttribute('aria-pressed', m === 'wait'); document.getElementById('mListen').setAttribute('aria-pressed', m === 'listen'); if (m === 'wait') t = onsets.filter(s => s <= t).pop() ?? onsets[0]; draw(); };
document.getElementById('mWait').onclick = () => setMode('wait');
document.getElementById('mListen').onclick = () => setMode('listen');
document.getElementById('play').onclick = e => { playing = !playing; last = null; e.target.textContent = playing ? 'השהיה' : 'ניגון'; if (playing) { if (mode !== 'listen') setMode('listen'); requestAnimationFrame(tick); } };
const bpmEl = document.getElementById('bpm'); bpmEl.textContent = bpm;
document.getElementById('slower').onclick = () => { bpm = Math.max(30, bpm - 6); bpmEl.textContent = bpm; draw(); };
document.getElementById('faster').onclick = () => { bpm = Math.min(200, bpm + 6); bpmEl.textContent = bpm; draw(); };
document.getElementById('midi').onclick = async e => { try { const names = await listener.connectMidi(); e.target.textContent = names.length ? 'MIDI: ' + names[0] : 'אין פסנתר מחובר'; } catch (err) { e.target.textContent = 'MIDI לא נתמך כאן'; } };
document.getElementById('mic').onclick = async e => { try { await listener.connectMic(); e.target.setAttribute('aria-pressed', 'true'); e.target.textContent = 'מקשיב'; } catch (err) { e.target.textContent = 'אין גישה למיקרופון'; } };
img.onload = draw; draw();
</script></html>"""


if __name__ == "__main__":
    if len(sys.argv) < 4:
        raise SystemExit("usage: python prototypes/play_along.py <page_clean.png> <page_clean.json> <page_clean.musicxml> [out.html]")
    out = Path(sys.argv[4]) if len(sys.argv) > 4 else Path(sys.argv[1]).with_name("play_along.html")
    build(Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]), out)
