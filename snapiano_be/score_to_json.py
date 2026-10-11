"""
Turns a MusicXML file into the simple note list the app plays and shows,
and flags measures whose total duration doesn't match the time signature.
The same module will later run inside the server, so keep it dependency-light.
"""
from __future__ import annotations

import difflib
import json
from dataclasses import dataclass, field, asdict
from pathlib import Path

from music21 import clef, converter, meter, stream, tempo

DEFAULT_BPM = 90
EPS = 0.01


@dataclass
class NoteEvent:
    midi: int
    name: str          # e.g. "E4", "F#3"
    start: float       # in quarter notes from the beginning of the piece
    dur: float         # in quarter notes
    hand: str          # "R" or "L"
    measure: int


@dataclass
class MeasureIssue:
    hand: str
    measure: int
    expected: float
    found: float


@dataclass
class ScoreSummary:
    parts: int = 0
    measures: int = 0
    notes: int = 0
    notes_right: int = 0
    notes_left: int = 0
    time_signature: str = ""
    key: str = ""
    bpm: float = DEFAULT_BPM
    issues: list[MeasureIssue] = field(default_factory=list)

    @property
    def measures_ok_pct(self) -> float:
        if not self.measures:
            return 0.0
        bad = len({i.measure for i in self.issues})
        return round(100 * (1 - bad / self.measures), 1)


def _hand_for_part(part: stream.Part, index: int, total: int) -> str:
    """Treble clef = right hand, bass clef = left hand; fall back to staff order."""
    clefs = part.flatten().getElementsByClass(clef.Clef)
    first = clefs.first() if clefs else None
    if first is not None:
        if first.sign == "G":
            return "R"
        if first.sign == "F":
            return "L"
    if total >= 2:
        return "R" if index == 0 else "L"
    return "R"


def _measure_content(m: stream.Measure) -> float:
    voices = list(m.voices) or [m]
    return max(sum(n.quarterLength for n in v.notesAndRests) for v in voices)


def _check_measures(part: stream.Part, hand: str) -> tuple[int, list[MeasureIssue]]:
    measures = list(part.getElementsByClass(stream.Measure))
    issues: list[MeasureIssue] = []
    for i, m in enumerate(measures):
        try:
            expected = m.barDuration.quarterLength
        except Exception:
            continue
        found = _measure_content(m)
        if abs(found - expected) < EPS:
            continue
        is_edge = i == 0 or i == len(measures) - 1
        if is_edge and found < expected:
            continue  # pickup bar at the start, or the matching short bar at the end
        issues.append(MeasureIssue(hand, int(m.number or i + 1), float(expected), float(found)))
    return len(measures), issues


def _bpm(score: stream.Score) -> float:
    marks = score.flatten().getElementsByClass(tempo.MetronomeMark)
    for mark in marks:
        if mark.number:
            return float(mark.getQuarterBPM() or mark.number)
    return DEFAULT_BPM


def _events_for_part(part: stream.Part, hand: str) -> list[NoteEvent]:
    events: list[NoteEvent] = []
    flat = part.stripTies().flatten()
    for el in flat.notes:
        if el.quarterLength <= 0:  # grace notes
            continue
        measure = int(el.measureNumber or 0)
        for p in el.pitches:
            events.append(NoteEvent(
                midi=p.midi,
                name=p.nameWithOctave,
                start=round(float(el.offset), 4),
                dur=round(float(el.quarterLength), 4),
                hand=hand,
                measure=measure,
            ))
    return events


def analyze(musicxml_path: str | Path) -> tuple[ScoreSummary, list[NoteEvent], stream.Score]:
    score = converter.parse(str(musicxml_path))
    parts = list(score.parts)
    summary = ScoreSummary(parts=len(parts), bpm=_bpm(score))

    ts = score.flatten().getElementsByClass(meter.TimeSignature).first()
    summary.time_signature = ts.ratioString if ts else ""
    try:
        summary.key = str(score.analyze("key"))
    except Exception:
        summary.key = ""

    events: list[NoteEvent] = []
    max_measures = 0
    for idx, part in enumerate(parts):
        hand = _hand_for_part(part, idx, len(parts))
        count, issues = _check_measures(part, hand)
        max_measures = max(max_measures, count)
        summary.issues.extend(issues)
        events.extend(_events_for_part(part, hand))

    events.sort(key=lambda e: (e.start, e.midi))
    summary.measures = max_measures
    summary.notes = len(events)
    summary.notes_right = sum(1 for e in events if e.hand == "R")
    summary.notes_left = summary.notes - summary.notes_right
    return summary, events, score


def to_app_json(summary: ScoreSummary, events: list[NoteEvent]) -> dict:
    """The format the app reads. Times are in quarter notes so BPM can change freely."""
    return {
        "version": 1,
        "bpm": summary.bpm,
        "timeSignature": summary.time_signature,
        "key": summary.key,
        "measures": summary.measures,
        "flaggedMeasures": sorted({i.measure for i in summary.issues}),
        "notes": [asdict(e) for e in events],
    }


def compare_to_reference(events: list[NoteEvent], ref_events: list[NoteEvent]) -> dict:
    """Rough accuracy against a correct MusicXML of the same page (e.g. from MuseScore)."""
    result = {}
    for hand in ("R", "L"):
        got = [e for e in events if e.hand == hand]
        ref = [e for e in ref_events if e.hand == hand]
        if not ref:
            continue
        pitch = difflib.SequenceMatcher(None, [e.midi for e in got], [e.midi for e in ref]).ratio()
        both = difflib.SequenceMatcher(
            None, [(e.midi, e.dur) for e in got], [(e.midi, e.dur) for e in ref]
        ).ratio()
        result[hand] = {"pitch_pct": round(100 * pitch, 1), "pitch_rhythm_pct": round(100 * both, 1)}
    return result


if __name__ == "__main__":
    import sys

    s, ev, _ = analyze(sys.argv[1])
    print(json.dumps(to_app_json(s, ev), ensure_ascii=False, indent=2)[:2000])
