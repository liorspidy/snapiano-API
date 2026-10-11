"""Developer diagnostics derived from what the pipeline already knows. Nothing here changes recognition.

Raw MIDI pitch and timing stay the source of truth; the note name is a derived display value.
"""
from __future__ import annotations

from .score_to_json import ScoreSummary

LETTERS = "CDEFGAB"
NATURAL = (0, 2, 4, 5, 7, 9, 11)  # semitones above C for C D E F G A B
SHARP_NAMES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")
ACCIDENTAL = {-2: "bb", -1: "b", 0: "", 1: "#", 2: "x"}  # x = double sharp


def note_name(midi: int, diatonic: int | None = None) -> str:
    """Readable name such as 'Eb4', 'F#3', 'C4'. `diatonic` is octave*7 + letter index (C=0..B=6), as read from the MusicXML
    step and octave, which keeps the spelling (Eb versus D#). Without it, or if it does not fit the pitch, sharps are used."""
    if diatonic is not None and diatonic >= 0:
        octave, letter = divmod(diatonic, 7)
        alter = midi - (octave + 1) * 12 - NATURAL[letter]
        if alter in ACCIDENTAL:
            return f"{LETTERS[letter]}{ACCIDENTAL[alter]}{octave}"
    return f"{SHARP_NAMES[midi % 12]}{midi // 12 - 1}"


def build_diagnostics(summary: ScoreSummary) -> dict:
    """What the bar check found, kept per bar instead of only as a list of bar numbers.

    `issues`: one entry per hand and bar whose notes do not add up to the time signature
    (expected and found length in quarter notes). The bars flagged for the user are exactly the bars in `issues`.
    """
    return {
        "version": 1,
        "barsChecked": summary.measures,
        "issues": [
            {"bar": i.measure, "hand": i.hand, "expected": round(i.expected, 4), "found": round(i.found, 4)}
            for i in sorted(summary.issues, key=lambda i: (i.measure, i.hand))
        ],
    }
