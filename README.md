# SLABCAST — an honest SoCal surf read

A free, honest surf forecast for Southern California (Ventura → San Onofre, 20 breaks).
Rebuilds itself every morning (~3 AM Pacific) via GitHub Actions and serves on GitHub Pages.
No ads. No logins. No paywall.

## What's here
- `surf_refresh.py` — the engine: pulls live data, bias-corrects toward measured buoys, scores every break, writes `index.html`.
- `surf_template.html` — the site design (the engine swaps only the data block).
- `surf_spots_static.json` — the 20 breaks (name, location, profile notes).
- `index.html` — the generated page that gets served.
- `.github/workflows/refresh.yml` — daily rebuild + deploy to Pages.

## Data sources (blended into one read)
Open-Meteo Marine (wave models) · NOAA NDBC buoys · CDIP/Scripps buoys · NOAA CO-OPS tides · Open-Meteo wind. All free, no keys.

## Notes
- Scoring is shape-first (swell fit + wind + tide); wave size shown is a deliberately conservative average face.
- The two community forms need a free Web3Forms key dropped into `surf_template.html` (replace `__WEB3FORMS_KEY__`) to start emailing submissions.
- Cron is UTC: `0 10 * * *` ≈ 3 AM Pacific. Edit in the workflow to change.
