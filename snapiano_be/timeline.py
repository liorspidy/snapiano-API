"""
Reads homr's MusicXML (with the <!-- imgpos: x, y --> comment homr writes in every note) and
returns one entry per sounding note: when it starts and how long it lasts (in quarter notes),
which hand, its MIDI number, and where it sits on the page image. That last part is what lets
the app put the cursor exactly on the printed notes of the cleaned scan.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass

STEP = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
LETTER = {"C": 0, "D": 1, "E": 2, "F": 3, "G": 4, "A": 5, "B": 6}
# diatonic number (octave * 7 + letter) of the BOTTOM staff line for each clef
CLEF_BOTTOM = {("G", 2): 4 * 7 + 2, ("F", 4): 2 * 7 + 4, ("C", 3): 3 * 7 + 3, ("C", 4): 3 * 7 + 1, ("G", 1): 4 * 7 + 4}


@dataclass
class TimedNote:
    midi: int
    start: float      # quarter notes from the beginning
    dur: float
    hand: str         # "R" (staff 1) or "L" (staff 2)
    measure: int
    x: int | None     # position on the page image the score was read from (rough)
    y: int | None
    staff: int = 1
    diatonic: int = 0      # octave * 7 + letter: where it sits on the staff
    bottom_line: int = 30  # diatonic number of the staff's bottom line under the current clef


def _parse(path: str) -> ET.Element:
    parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True))
    return ET.parse(path, parser=parser).getroot()


def read_timeline(path: str) -> list[TimedNote]:
    root = _parse(path)
    notes: list[TimedNote] = []
    open_ties: dict[tuple[int, str], TimedNote] = {}
    for part in root.findall("part"):
        divisions, t = 1.0, 0.0
        clef_bottom = {1: CLEF_BOTTOM[("G", 2)], 2: CLEF_BOTTOM[("F", 4)]}
        for measure in part.findall("measure"):
            number = int(re.sub(r"\D", "", measure.get("number", "0")) or 0)
            last_start = t
            for el in measure:
                if el.tag == "attributes":
                    if el.find("divisions") is not None:
                        divisions = float(el.find("divisions").text)
                    for c in el.findall("clef"):
                        key = (c.findtext("sign"), int(c.findtext("line") or 0))
                        if key in CLEF_BOTTOM:
                            clef_bottom[int(c.get("number", "1"))] = CLEF_BOTTOM[key]
                elif el.tag == "backup":
                    t -= float(el.find("duration").text) / divisions
                elif el.tag == "forward":
                    t += float(el.find("duration").text) / divisions
                elif el.tag == "note":
                    if el.find("grace") is not None or el.find("duration") is None:
                        continue
                    dur = float(el.find("duration").text) / divisions
                    chord = el.find("chord") is not None
                    start = last_start if chord else t
                    if not chord:
                        last_start = t
                        t += dur
                    pitch = el.find("pitch")
                    if pitch is None:  # rest
                        continue
                    midi = (int(pitch.find("octave").text) + 1) * 12 + STEP[pitch.find("step").text]
                    if pitch.find("alter") is not None:
                        midi += int(float(pitch.find("alter").text))
                    staff = int(el.findtext("staff") or "1")
                    hand = "L" if staff == 2 else "R"
                    diatonic = int(pitch.find("octave").text) * 7 + LETTER[pitch.find("step").text]
                    x = y = None
                    for child in el:
                        if child.tag is ET.Comment and "imgpos" in (child.text or ""):
                            x, y = (int(v) for v in re.findall(r"\d+", child.text)[:2])
                    ties = {tie.get("type") for tie in el.findall("tie")}
                    key = (midi, hand)
                    if "stop" in ties and key in open_ties:  # continuation of a held note
                        open_ties[key].dur = round(start + dur - open_ties[key].start, 4)
                        if "start" not in ties:
                            open_ties.pop(key)
                        continue
                    n = TimedNote(midi, round(start, 4), round(dur, 4), hand, number, x, y,
                                  staff, diatonic, clef_bottom.get(staff, 30))
                    notes.append(n)
                    if "start" in ties:
                        open_ties[key] = n
    notes.sort(key=lambda n: (n.start, n.midi))
    return notes


if __name__ == "__main__":
    import sys

    tl = read_timeline(sys.argv[1])
    print(len(tl), "notes;", sum(1 for n in tl if n.x is not None), "with positions")
    for n in tl[:12]:
        print(n)
