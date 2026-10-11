#!/usr/bin/env bash
# Downloads the public-domain test pages used in the experiments into BE/pages/.
set -e
cd "$(dirname "$0")/../pages"
B=https://raw.githubusercontent.com/Audiveris/audiveris/master
curl -fsSL -o BachInvention5.jpg "$B/data/examples/BachInvention5.jpg"          # phone photo of a printed page
curl -fsSL -o allegretto.png    "$B/data/examples/allegretto.png"               # clean scan
curl -fsSL -o moonlight_p1.png  "$B/docs/_pages/guides/ui/ui_examples/moonlight/sheet1_raw.png"  # low-res scan
echo "Saved to $(pwd)"
