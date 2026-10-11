# Experiments (October 2026)

All runs on a CPU-only Linux machine (4 GB RAM). Pages: `scripts/get_test_pages.sh`.

## OMR (homr from GitHub main, AGPL-3.0)

| Page | Type | Time | Bars | Bars adding up |
|---|---|---|---|---|
| Bach Invention 5 | phone photo of print | 46-48 s | 17 | 100% |
| Allegretto | clean scan | 27-45 s | 17 | 100% |
| Moonlight p.1 | low-res scan (781x621) | 28-30 s | 11 | 90.9% (bar 5) |
| Phone photo (private test page, not distributed) | photo | 42 s | 28 | 100% |

- Raw vs cleaned input: note sequences ~identical on decent photos (homr has its own preprocessing).
  Cleaning matters for **display** and for bad photos.
- Bad photo test (Bach + strong shadow, warm tint, 2.5 deg tilt, noise, JPEG 60): readings of raw and
  cleaned versions matched the good-photo reading: right hand 100%, left hand 98.5% (pitch sequence).
- Low-res scan + bad light: fails raw and cleaned. Must be rejected at capture (quality check flags it "poor").
- Bars-adding-up is a weak metric (catches missing notes/durations, not wrong pitches). The pitch-based
  note marks on the cleaned image give a visual check: marks land on the printed noteheads.

## oemer (MIT) — not evaluated
1. Fails with current onnxruntime (ConvTranspose negative pads). Works with onnxruntime 1.17.3 + numpy<2,
   scipy<1.14, scikit-learn<1.6 in a separate venv.
2. Then killed by the OOM killer at ~4 GB RSS, also on a page reduced to 1300 px wide.
   Needs a machine with >= 8 GB RAM to test.

## Cleaning (snapiano_be/cleaning.py)
- Rotation found on the bad photo: -2.2 deg (true 2.5 deg); staves found on all pages (5-strip voting).
- Dewarp test: artificial 18 px bow on a line -> staff-line straightness std 6.72 px before, 0.36 px after.

## Microphone note check (tools/listen_check.py, same algorithm as FE/src/audio/listen.js)
FluidR3 GM piano samples (MIT, `scripts/get_piano_samples.sh`), random single notes and chords with
previous notes ringing, simulated room echo and noise, 400 trials per SNR:

| SNR | Correct notes accepted | Wrong notes caught | Wrong note named (pitch class) |
|---|---|---|---|
| 30 dB | 88.4% | 93.0% | 56.5% |
| 20 dB | 88.8% | 93.7% | 58.8% |
| 12 dB | 90.1% | 94.3% | 61.1% |

Decision delay ~190 ms after the key is struck. The JS port gives the same decisions as Python on test
cases. **Not yet tested with a real phone microphone and a real piano.**
