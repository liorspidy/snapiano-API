"""
Score-informed listening: we already know which notes should come next, so instead of
transcribing everything (hard for chords), we only ask "did the expected notes just start?"
That is fast enough to run on the phone with no server, and works for chords.

The same algorithm is implemented in JavaScript in live_demo.html (Web Audio), which maps
directly onto react-native-audio-api in the app.

Evaluation (from BE/): python tools/listen_check.py data/piano_samples
(samples named like C4.mp3, Db4.mp3 ... e.g. FluidR3 from gleitz/midi-js-soundfonts)
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

N_FFT = 8192        # ~170 ms at 48 kHz: enough resolution to separate semitones down to C3
HOP = 512           # ~11 ms between onset checks
HARMONICS = 8
# decision thresholds (dB / ratios), tuned on real piano samples with room noise
TUNE = {"min_db": 4.0, "near": 1.5, "far": 0.5, "odd": 0.45, "lower": 0.6}
NAMES = ["C", "Db", "D", "Eb", "E", "F", "Gb", "G", "Ab", "A", "Bb", "B"]


def midi_hz(m: float) -> float:
    return 440.0 * 2 ** ((m - 69) / 12)


def name(m: int) -> str:
    return f"{NAMES[m % 12]}{m // 12 - 1}"


class Listener:
    def __init__(self, sr: int):
        self.sr = sr
        self.win = np.hanning(N_FFT).astype(np.float32)
        self.small_win = np.hanning(2048).astype(np.float32)
        self.prev_small = None
        self.flux_hist: list[float] = []
        self.broad_rise = 0.0
        self.tune = dict(TUNE)

    # --- spectra ---
    def spectrum(self, x: np.ndarray) -> np.ndarray:
        frame = np.zeros(N_FFT, np.float32)
        frame[:len(x)] = x[:N_FFT]
        return 20 * np.log10(np.abs(np.fft.rfft(frame * self.win)) + 1e-7)  # dB, not normalized

    def harmonic_bins(self, notes: list[int]) -> set[int]:
        out = set()
        for m in notes:
            for h in range(1, HARMONICS + 1):
                b = int(round(midi_hz(m) * h * N_FFT / self.sr))
                out.update(range(b - 2, b + 3))
        return out

    def gain(self, s0: np.ndarray, s1: np.ndarray, floor: float, m: float, odd_only: bool = False,
             skip: set[int] | None = None) -> float:
        """How many dB the harmonics of note m rose since just before the onset."""
        total, weight = 0.0, 0.0
        for h in range(1, HARMONICS + 1):
            if odd_only and h % 2 == 0:
                continue
            b = midi_hz(m) * h * N_FFT / self.sr
            if b >= len(s1) - 2 or midi_hz(m) * h > 6000:
                break
            i = int(round(b))
            if skip and i in skip:  # this harmonic is shared with a note that should sound: no evidence
                continue
            j = i - 1 + int(np.argmax(s1[i - 1:i + 2]))
            after, before = s1[j], s0[i - 1:i + 2].max()
            side = np.r_[s1[max(0, j - 7):j - 3], s1[j + 4:j + 8]].mean()
            w = 1 / h ** 0.5
            weight += w
            # count a harmonic only if it is a real spectral peak, clearly above the room noise
            if after > floor + 12 and after - side > 6:
                total += w * min(30.0, max(0.0, after - before - self.broad_rise))
        return total / weight if weight else 0.0

    # --- onsets (spectral flux on a short window) ---
    def is_onset(self, frame: np.ndarray) -> bool:
        mag = np.log1p(np.abs(np.fft.rfft(frame * self.small_win)))
        if self.prev_small is None:
            self.prev_small = mag
            return False
        flux = float(np.maximum(mag - self.prev_small, 0).sum())
        self.prev_small = mag
        hist = self.flux_hist[-40:]
        thr = (np.median(hist) * 2.5 + 5) if len(hist) > 5 else 1e9
        self.flux_hist.append(flux)
        return flux > thr

    # --- the decision ---
    def check(self, before: np.ndarray, after: np.ndarray, expected: list[int]) -> tuple[bool, list[int], int | None]:
        """before/after: audio just before and just after the onset. Returns
        (all expected notes present, which were found, best guess of the note actually played)."""
        s0, s1 = self.spectrum(before), self.spectrum(after)
        lo, hi = int(80 * N_FFT / self.sr), int(5000 * N_FFT / self.sr)
        floor = float(np.median(s1[lo:hi]))
        # the hammer's thump raises everything a little; only rises above that count
        self.broad_rise = float(np.median(np.maximum(0, s1[lo:hi] - s0[lo:hi])))
        g = lambda m, odd=False, skip=None: self.gain(s0, s1, floor, m, odd, skip)
        shared = self.harmonic_bins(expected)
        found = []
        for m in expected:
            d = g(m)
            # rivals are judged only on harmonics no expected note explains
            near = [g(n, skip=shared) for n in (m - 1, m + 1) if n not in expected]
            far = [g(n, skip=shared) for n in (m - 2, m + 2) if n not in expected]
            lower = g(m - 12, True, shared) if m - 12 not in expected else 0.0
            # present = its harmonics rose clearly, more than its neighbours did,
            # its odd harmonics rose too (not the octave above),
            # and the octave below did not start (its odd harmonics would rise)
            t = self.tune
            if (d > t["min_db"] and all(d > r + t["near"] for r in near) and all(d > r + t["far"] for r in far)
                    and g(m, True) > t["odd"] * d and lower < t["lower"] * d):
                found.append(m)
        guess = None
        if len(found) < len(expected):
            cands = list(range(36, 97))
            scores = [g(m) + g(m, True) for m in cands]
            guess = cands[int(np.argmax(scores))]
        return len(found) == len(expected), found, guess


# ---------- offline evaluation with real piano recordings ----------

def load_samples(folder: Path, sr: int) -> dict[int, np.ndarray]:
    import soundfile as sf
    out = {}
    for f in folder.glob("*.mp3"):
        n, octv = f.stem[:-1], int(f.stem[-1])
        if n not in NAMES:
            continue
        x, fsr = sf.read(str(f), dtype="float32")
        x = x.mean(1) if x.ndim == 2 else x
        if fsr != sr:  # simple resample
            t = np.arange(0, len(x) / fsr, 1 / sr)
            x = np.interp(t, np.arange(len(x)) / fsr, x).astype(np.float32)
        out[(octv + 1) * 12 + NAMES.index(n)] = x
    return out


def room(x: np.ndarray, sr: int, rng) -> np.ndarray:
    ir_len = int(0.35 * sr)
    ir = rng.normal(0, 1, ir_len) * np.exp(-np.arange(ir_len) / (0.08 * sr))
    ir[0] = 12
    y = np.convolve(x, ir / np.abs(ir).sum() * 3)[: len(x)]
    # phone mic: weak lows, weak highs
    from numpy.fft import irfft, rfft
    Y = rfft(y)
    f = np.fft.rfftfreq(len(y), 1 / sr)
    Y *= (1 / np.sqrt(1 + (120 / np.maximum(f, 1)) ** 4)) * (1 / np.sqrt(1 + (f / 7000) ** 4))
    return irfft(Y, len(y)).astype(np.float32)


def evaluate(folder: Path, trials: int = 400, snr_db: float = 20, seed: int = 0, tune: dict | None = None) -> dict:
    sr = 48000
    rng = np.random.default_rng(seed)
    samples = load_samples(folder, sr)
    keys = sorted(samples)
    lo, hi = keys[0], keys[-1]
    stats = {"correct_played": 0, "accepted": 0, "wrong_played": 0, "rejected": 0, "guess_right": 0}
    L = Listener(sr)
    if tune:
        L.tune.update(tune)
    pre = int(0.4 * sr)
    for _ in range(trials):
        size = rng.choice([1, 1, 2, 3])
        root = int(rng.integers(lo + 2, hi - 14))
        expected = sorted({root + int(i) for i in rng.choice([0, int(rng.choice([3, 4])), 7, 12], size, replace=False)})
        expected = [m for m in expected if m in samples]
        wrong = rng.random() < 0.4
        played = list(expected)
        if wrong:
            k = int(rng.integers(len(played)))
            shift = int(rng.choice([-2, -1, 1, 2, 12, -12]))
            if played[k] + shift in samples:
                played[k] += shift
            else:
                wrong = False
        # the previous notes are still ringing when the new ones start
        prev = [int(rng.integers(lo, hi)) for _ in range(int(rng.integers(0, 3)))]
        x = np.zeros(pre + int(0.5 * sr), np.float32)
        for m in prev:
            s = samples[m][: len(x)] * 0.6
            if m in played:  # to strike a key again it has to come up: the damper stops the string
                t = np.arange(len(s)) / sr
                s = s * np.where(t < 0.28, 1.0, np.exp(-(t - 0.28) / 0.03))
            x[: len(s)] += s
        for m in played:
            s = samples[m][: len(x) - pre] * float(10 ** (rng.uniform(-6, 0) / 20))
            x[pre: pre + len(s)] += s
        x = room(x, sr, rng)
        x += rng.normal(0, np.sqrt(np.mean(x ** 2)) / 10 ** (snr_db / 20), len(x)).astype(np.float32)

        before = x[pre - N_FFT: pre]
        after = x[pre + int(0.02 * sr): pre + int(0.02 * sr) + N_FFT]
        ok, _, guess = L.check(before, after, expected)
        if wrong:
            stats["wrong_played"] += 1
            if not ok:
                stats["rejected"] += 1
                diff = [m for m in played if m not in expected]
                if guess is not None and diff and guess % 12 == diff[0] % 12:
                    stats["guess_right"] += 1
        else:
            stats["correct_played"] += 1
            stats["accepted"] += ok
    return {
        "snr_db": snr_db,
        "correct notes accepted": f"{100 * stats['accepted'] / max(1, stats['correct_played']):.1f}%",
        "wrong notes caught": f"{100 * stats['rejected'] / max(1, stats['wrong_played']):.1f}%",
        "wrong note named (pitch class)": f"{100 * stats['guess_right'] / max(1, stats['rejected']):.1f}%",
        "decision delay": f"{(0.02 + N_FFT / sr) * 1000:.0f} ms after the key is struck",
    }


if __name__ == "__main__":
    folder = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("data/piano_samples")
    for snr in (30, 20, 12):
        print(evaluate(folder, snr_db=snr))
