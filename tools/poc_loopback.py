"""Timing and click analysis of a loopback recording of the audio PoC (FE/src/app/audio-poc.tsx).

Record the phone's output by cable (headphone out or USB-C DAC -> computer line in, never the speaker), play one PoC
test, pull its JSON log from the phone, then:

    python tools/poc_loopback.py recording.wav Aprime-steady60-123.json
    python tools/poc_loopback.py --selftest

Expected onsets are the times the app scheduled (audio clock), so tempo changes are covered. The constant output
latency is removed; what is left is jitter (per-note error), drift (ppm) and missed or extra notes. Clicks are
sudden steps in the waveform away from note onsets. These are evidence, not a substitute for listening.
"""
import json
import sys

import numpy as np

PASS = {"p99_ms": 2.0, "drift_ppm": 50.0}


def onsets(x, sr, rise=1.8, refractory=0.04):
    hop = max(1, sr // 1000)  # 1 ms
    win = 5 * hop  # longer than one period of the lowest test note, so the envelope does not ripple
    a = np.abs(x)
    n = len(x) // hop
    env = np.array([a[max(0, i * hop - win) : i * hop + 1].max() for i in range(n)])
    floor = env.max() * 10 ** (-40 / 20)
    found, last = [], -1e9
    for i in range(8, n):
        prev = env[i - 8]  # 8 ms earlier: the tail of the previous note
        if env[i] > floor and env[i] > rise * prev + 1e-9 and (i - last) * hop / sr > refractory:
            # the exact onset: first sample in the last 8 ms above halfway between the old tail and the new peak
            lo = (i - 8) * hop
            seg = a[lo : i * hop + 1]
            k = int(np.argmax(seg > prev + 0.5 * (env[i] - prev)))
            found.append((lo + k) / sr)
            last = i
    return np.array(found)


def clicks(x, sr, near, guard=0.015, factor=30.0):
    d2 = np.abs(np.diff(x, 2))
    win = max(8, sr // 100)
    local = np.convolve(d2, np.ones(win) / win, mode="same") + 1e-9
    hits = np.where(d2 > factor * local)[0] / sr
    if len(near):
        hits = np.array([h for h in hits if np.min(np.abs(near - h)) > guard])
    merged = []
    for h in hits:
        if not merged or h - merged[-1] > 0.005:
            merged.append(h)
    return merged


def analyse(x, sr, expected, tolerance=0.03):
    det = onsets(x, sr)
    exp = np.unique(np.round(np.sort(expected), 3))  # a chord is one onset
    exp = exp - exp[0]
    if not len(det):
        return {"error": "no onsets found"}
    offset = det[0] - exp[0]
    for _ in range(2):  # match, re-estimate the latency from the median, match again
        idx = np.searchsorted(det, exp + offset)
        pairs = []
        for j, e in enumerate(exp + offset):
            cand = [c for c in (idx[j] - 1, idx[j]) if 0 <= c < len(det)]
            c = min(cand, key=lambda c: abs(det[c] - e)) if cand else None
            if c is not None and abs(det[c] - e) < tolerance:
                pairs.append((j, c))
        err = np.array([det[c] - (exp[j] + offset) for j, c in pairs])
        offset += np.median(err) if len(err) else 0
    err = np.array([det[c] - (exp[j] + offset) for j, c in pairs])
    t = np.array([exp[j] for j, _ in pairs])
    slope = np.polyfit(t, err, 1)[0] if len(t) > 2 and np.ptp(t) > 1 else 0.0
    used = {c for _, c in pairs}
    res = {
        "expected": int(len(exp)),
        "matched": len(pairs),
        "missed": int(len(exp) - len(pairs)),
        "extra": int(len(det) - len(used)),
        "latency_ms_relative_to_first": float(offset * 1000),
        "err_p50_ms": float(np.median(np.abs(err)) * 1000),
        "err_p99_ms": float(np.quantile(np.abs(err), 0.99) * 1000),
        "err_max_ms": float(np.abs(err).max() * 1000),
        "drift_ppm": float(slope * 1e6),
        "clicks": [round(float(c), 3) for c in clicks(x, sr, det)],
    }
    res["pass"] = (
        res["missed"] == 0
        and res["extra"] == 0
        and res["err_p99_ms"] <= PASS["p99_ms"]
        and abs(res["drift_ppm"]) <= PASS["drift_ppm"]
        and not res["clicks"]
    )
    return res


def load(wav, log):
    import soundfile as sf

    x, sr = sf.read(wav)
    x = x.mean(axis=1) if x.ndim > 1 else x
    run = json.load(open(log))
    expected = [e["t"] for e in run["scheduled"] if not e.get("cancelled")]
    return x, sr, expected


def tone(sr, length):
    t = np.arange(int(length * sr)) / sr
    fade = np.clip((length - t) / 0.01, 0, 1)  # 10 ms release, like the engine's damper
    return np.sin(2 * np.pi * 262 * t) * np.exp(-t * 6) * np.minimum(1, t * sr / 20) * fade


def selftest():
    rng = np.random.default_rng(0)
    sr, ppm, latency = 48000, 20, 0.08
    exp = np.arange(240) * 0.125
    x = np.zeros(int((exp[-1] + 1) * sr))
    note = tone(sr, 0.12)
    for e in exp:
        at = int((latency + e * (1 + ppm * 1e-6) + rng.normal(0, 0.0002)) * sr)
        x[at : at + len(note)] += note * 0.3
    x += rng.normal(0, 1e-4, len(x))
    r = analyse(x, sr, exp)
    assert r["matched"] == 240 and r["missed"] == 0 and r["extra"] == 0, r
    assert abs(r["drift_ppm"] - ppm) < 10, r
    assert r["err_p99_ms"] < 1.5, r
    assert not r["clicks"], r
    for c in (3.3, 10.03, 20.61):  # away from note onsets (80 ms + k * 125 ms)
        x[int(c * sr)] += 0.2
    r = analyse(x, sr, exp)
    assert len(r["clicks"]) >= 3, r
    x2 = x.copy()  # a missing note
    k = int((latency + exp[100]) * sr)
    x2[k - 200 : k + len(note)] = 0
    assert analyse(x2, sr, exp)["missed"] >= 1
    print("selftest ok")


if __name__ == "__main__":
    if sys.argv[1:] == ["--selftest"]:
        selftest()
    elif len(sys.argv) == 3:
        print(json.dumps(analyse(*load(*sys.argv[1:])), indent=2))
    else:
        print(__doc__)
