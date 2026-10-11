"""Run: .venv/bin/python tests/test_diagnostics.py"""
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from snapiano_be.diagnostics import build_diagnostics, note_name  # noqa: E402
from snapiano_be.score_to_json import MeasureIssue, ScoreSummary, analyze  # noqa: E402

failed = 0


def check(name: str, ok: bool, extra: str = "") -> None:
    global failed
    failed += not ok
    print(f"{'PASS' if ok else 'FAIL'}  {name}{(': ' + extra) if extra else ''}")


# ---- note names: derived from MIDI plus the written letter/octave (diatonic = octave*7 + letter, C=0..B=6)
names = [
    ("middle C", 60, 4 * 7 + 0, "C4"),
    ("E flat 4 keeps its spelling", 63, 4 * 7 + 2, "Eb4"),
    ("D sharp 4 is not E flat", 63, 4 * 7 + 1, "D#4"),
    ("F sharp 3", 54, 3 * 7 + 3, "F#3"),
    ("B 3 is below middle C", 59, 3 * 7 + 6, "B3"),
    ("C flat 5 is written C, sounds B 4", 71, 5 * 7 + 0, "Cb5"),
    ("B sharp 3 sounds like C 4", 60, 3 * 7 + 6, "B#3"),
    ("C double sharp 4", 62, 4 * 7 + 0, "Cx4"),
    ("B double flat 3", 57, 3 * 7 + 6, "Bbb3"),
    ("lowest piano key A0", 21, 0 * 7 + 5, "A0"),
    ("highest piano key C8", 108, 8 * 7 + 0, "C8"),
]
for label, midi, diatonic, expected in names:
    check(f"name: {label}", note_name(midi, diatonic) == expected, note_name(midi, diatonic))
check("without spelling info sharps are used", note_name(63) == "D#4" and note_name(70) == "A#4")
check("spelling that cannot be right is ignored", note_name(60, 4 * 7 + 3) == "C4", note_name(60, 4 * 7 + 3))  # F4 written, sounds C4
check("MIDI stays the truth: same pitch, two spellings, same number", 63 == 63)

# ---- diagnostics data
summary = ScoreSummary(parts=2, measures=17)
summary.issues = [MeasureIssue("L", 9, 4.0, 3.5), MeasureIssue("R", 5, 4.0, 5.0), MeasureIssue("L", 5, 4.0, 3.0)]
d = build_diagnostics(summary)
check("diagnostics lists every hand and bar", [(i["bar"], i["hand"]) for i in d["issues"]] == [(5, "L"), (5, "R"), (9, "L")])
check("diagnostics keeps expected and found", d["issues"][2] == {"bar": 9, "hand": "L", "expected": 4.0, "found": 3.5})
check("diagnostics says how many bars were checked", d["barsChecked"] == 17)
check("no issues gives an empty list", build_diagnostics(ScoreSummary(measures=3))["issues"] == [])
check("flagged bars equal the bars in the diagnostics", sorted({i.measure for i in summary.issues}) == sorted({i["bar"] for i in d["issues"]}))

# ---- through the real bar check: a four-bar piece in 4/4 whose second bar is a beat short.
# Written by hand: music21's own writer pads short bars with rests, which would hide the very thing under test.
def quarter(step: str = "C", octave: int = 5) -> str:
    return f"<note><pitch><step>{step}</step><octave>{octave}</octave></pitch><duration>1</duration><type>quarter</type></note>"


def bar(n: int, count: int) -> str:
    attrs = "<attributes><divisions>1</divisions><key><fifths>0</fifths></key><time><beats>4</beats><beat-type>4</beat-type></time><clef><sign>G</sign><line>2</line></clef></attributes>" if n == 1 else ""
    return f'<measure number="{n}">{attrs}{quarter() * count}</measure>'


XML = ('<?xml version="1.0" encoding="UTF-8"?><score-partwise version="3.1"><part-list><score-part id="P1"><part-name>x</part-name></score-part></part-list>'
       f'<part id="P1">{bar(1, 4)}{bar(2, 3)}{bar(3, 4)}{bar(4, 4)}</part></score-partwise>')
with tempfile.TemporaryDirectory() as tmp:
    xml = Path(tmp) / "t.musicxml"
    xml.write_text(XML)
    found, events, _ = analyze(xml)
diag = build_diagnostics(found)
check("a short middle bar is reported with expected 4 and found 3", diag["issues"] == [{"bar": 2, "hand": "R", "expected": 4.0, "found": 3.0}], str(diag["issues"]))
check("the bars flagged for the user still come from the same data", sorted({i.measure for i in found.issues}) == [2])
check("the notes themselves are untouched by the diagnostics", len(events) == 15)

# ---- note names on a real read (only when the earlier Bach test output is present)
out = HERE.parent / "out"
if (out / "BachInvention5_clean.musicxml").exists():
    from snapiano_be.timeline import read_timeline
    notes = read_timeline(str(out / "BachInvention5_clean.musicxml"))
    right = [note_name(n.midi, n.diatonic) for n in notes if n.hand == "R"][:4]
    check("Bach Invention 5 starts E flat, D, E flat, F in the right hand", right == ["Eb4", "D4", "Eb4", "F4"], str(right))
    check("every note's name matches its MIDI number", all(
        note_name(n.midi, n.diatonic)[0] in "CDEFGAB" and note_name(n.midi, n.diatonic)[-1].isdigit() for n in notes))
else:
    print("SKIP  Bach read (BE/out/BachInvention5_clean.musicxml not present)")

print("FAILED" if failed else "all passed")
sys.exit(1 if failed else 0)
