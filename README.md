# Snapiano backend

Run everything from this folder with Python 3.11 or 3.12.

```
pip install -r requirements.txt
scripts/get_test_pages.sh
python tools/run_omr_test.py                      # benchmark -> results/report.html
python -m snapiano_be.cleaning pages/BachInvention5.jpg out/
homr out/BachInvention5_clean.png
python tools/play_along.py out/BachInvention5_clean.png out/BachInvention5_clean.json out/BachInvention5_clean.musicxml out/play.html
```
Database: run `supabase/migrations/001..003` in order in the Supabase SQL editor.
Results so far: `docs/experiments.md`.
# snapiano-API
