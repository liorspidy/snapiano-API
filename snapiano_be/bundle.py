"""Clean page + homr timeline -> per-system straightened line images and note marks [midi, start, dur, hand, x, y]."""
from __future__ import annotations

import numpy as np

from .cleaning import find_staves, snap_to_noteheads, straighten_band_with_shift
from .diagnostics import note_name
from .timeline import TimedNote


def build_lines(page: np.ndarray, systems: list[list[int]], notes: list[TimedNote]) -> tuple[list[np.ndarray], list[dict]]:
    """Returns (line images, note dicts). Each note dict: m, s, d, h, line (1-based), x, y (in that line image)."""
    notes = [n for n in notes if n.x is not None]
    images, data = [], []
    for i, (y0, y1) in enumerate(systems, 1):
        img, shift = straighten_band_with_shift(page[y0:y1])
        images.append(img)
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
            data.append({"m": n.midi, "s": n.start, "d": n.dur, "h": n.hand, "line": i, "b": n.measure, "n": note_name(n.midi, n.diatonic),
                         "x": round(float(x), 1), "y": round(float(y), 1)})
    data.sort(key=lambda d: (d["s"], d["m"]))
    return images, data
