#!/usr/bin/env bash
# FluidR3 GM acoustic grand piano note samples (MIT) for tools/listen_check.py
set -e
mkdir -p "$(dirname "$0")/../data/piano_samples" && cd "$(dirname "$0")/../data/piano_samples"
B=https://raw.githubusercontent.com/gleitz/midi-js-soundfonts/gh-pages/FluidR3_GM/acoustic_grand_piano-mp3
for o in 1 2 3 4 5 6; do for p in C Db D Eb E F Gb G Ab A Bb B; do curl -fsSL -o "$p$o.mp3" "$B/$p$o.mp3"; done; done
curl -fsSL -o C7.mp3 "$B/C7.mp3"; echo "Saved $(ls | wc -l) samples"
