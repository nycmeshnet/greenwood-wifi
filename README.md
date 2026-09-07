# Green-Wood Cemetery WiFi AP Placement Optimizer

Calculates the minimum number of solar-powered WiFi access points needed to cover Green-Wood Cemetery in Brooklyn, NY, and outputs their optimal locations.

## What it does

Given the rated range of an access point on each frequency band, the tool:

1. Fetches cemetery topology, tree cover, and building footprints from OpenStreetMap
2. Downloads a 5 m elevation raster from USGS 3DEP
3. Pulls multi-year monthly solar irradiance from NASA POWER
4. Models RF propagation using ITU-R P.833 vegetation attenuation
5. Checks solar viability for each candidate location (100 W panel, 10 W load, 50 Wh battery)
6. Solves an Integer Linear Program (scipy/HiGHS) to minimize AP count
7. Writes results as JSON, a Google Earth KML overlay, and a Markdown summary

**No API keys required.** All data sources are free and open.

## Zero-host cloud run (no install, no server to manage)

GitHub does the compute and hosts the map — no Docker, no VPS, no API keys.

### Option A — run in the cloud via Actions (fully featured)

1. Fork the repo (or use this repo if you have access).
2. Go to **Actions → Run optimizer (zero-host) → Run workflow**.
3. Fill the form — every field maps 1:1 to a `main.py` CLI flag:

| Input | Default | CLI equiv |
|---|---|---|
| range-2g / range-5g (ft) | 2000 / 1250 | `--range-2g` / `--range-5g` |
| range-6g (ft, empty=off) | empty | `--range-6g` |
| coverage | full | `--coverage full\|paths` |
| relation-id | 1370699 (Green-Wood) | `--relation-id` |
| panel-w / load-w / derating | 100 / 10 / 0.80 | `--panel-w` / `--load-w` / `--derating` |
| tilt / freeze-thresh-f | 40 / 34 | `--tilt` / `--freeze-thresh-f` |
| candidate-spacing / ilp-spacing / test-spacing (m, advanced) | 20 / 20 / 10 | `--candidate-spacing` etc. |
| mount-height / client-height (m) | 2.0 / 2.0 | `--mount-height-m` / `--client-height-m` |
| shade-radius (m, advanced, 0=auto) | 50 | `--shade-radius` |
| marginal-penalty (advanced) | 1.5 | `--marginal-penalty` |
| refresh-cache | false | force live re-fetch (`--no-cache`) even when cache is fresh |
| publish | true | deploy results to the GitHub Pages site |

4. Click **Run workflow**. An `ubuntu-latest` runner installs `requirements.txt` and runs `python main.py …`. Typical run: 5–10 min cold on Actions (dependency install + full solve); ~1 min locally with a warm `data/cache/`.
5. When green, download results from the run page under **Artifacts** (`ap-placement-<N>`, 90-day retention): `ap_placement.json`, `ap_placement.kml`, `summary.md`.
6. If `publish=true`, the same files + viewer are deployed to the GitHub Pages site at
   [https://nycmeshnet.github.io/greenwood-wifi/](https://nycmeshnet.github.io/greenwood-wifi/) (one-time setup: Settings → Pages → Source: **GitHub Actions**).

Public repos get effectively unlimited Actions minutes for this.

_Forked this repo? Replace `nycmeshnet/greenwood-wifi` in the links above with your `org/repo` — the workflow, viewer, and Pages deploy are org-agnostic and work unchanged._

### Option B — view results (no compute)

* **Hosted map:** open [https://nycmeshnet.github.io/greenwood-wifi/](https://nycmeshnet.github.io/greenwood-wifi/). Green = viable, yellow = marginal. Table + per-AP Google Maps links included.
* **Local file:** open `viewer/index.html` directly in a browser (double-click works). It tries `./ap_placement.json`, then `../output/ap_placement.json`. Use the file picker to load any run's JSON.
* **Google Earth:** Earth Web has no `?kml=` deep-link, so download `ap_placement.kml` and drag it into [earth.google.com](https://earth.google.com). In Earth Desktop use Add → Network Link with [https://nycmeshnet.github.io/greenwood-wifi/ap_placement.kml](https://nycmeshnet.github.io/greenwood-wifi/ap_placement.kml).
* **Other open tools:** `Open in geojson.io` button (works for ~40 points), or uMap → Import from URL with [https://nycmeshnet.github.io/greenwood-wifi/ap_placement.json](https://nycmeshnet.github.io/greenwood-wifi/ap_placement.json).

## Hardware assumptions (per AP node, all tunable via flags)

| Parameter | Default | Flag |
|---|---|---|
| Solar panel | 100 W rated | `--panel-w` |
| Load | 10 W continuous | `--load-w` |
| Battery | 50 Wh | (tracked, not in harvest calc) |
| Derating factor | 0.80 (temperature, wiring, soiling) | `--derating` |
| Panel tilt | 40° | `--tilt` |
| Design basis | Worst above-freezing month with sufficient sun (~October for Brooklyn) | auto from NASA POWER |
| Thermal cutoff | Below ~34 °F average monthly temperature | `--freeze-thresh-f` |

## Usage (local)

```bash
# Quick start using Makefile (2000 ft / 1250 ft defaults)
make run

# Or with venv active:

# Default ranges (2000 ft 2.4 GHz / 1250 ft 5 GHz)
python3 main.py

# Override ranges
python3 main.py --range-2g 500 --range-5g 260

# With 6 GHz tri-band AP
python3 main.py --range-6g 200

# Cover walkable paths only (faster, fewer APs)
python3 main.py --coverage paths

# Different site / hardware
python3 main.py --relation-id 1370699 --panel-w 120 --load-w 8 --tilt 35

# Re-fetch all external data
python3 main.py --no-cache

# Full tunable list
python3 main.py --help
```

All range values are in **feet**. Grid/heights are in **metres**.

### Outputs

| File | Description |
|---|---|
| `output/ap_placement.json` | AP list with lat/lon and name |
| `output/ap_placement.kml` | Google Earth overlay — open directly |
| `output/summary.md` | AP count, solar-marginal sites, efficiency flags |

The KML pins are colour-coded: **green** = solar viable, **yellow** = marginal (may need a better-sited panel).

## Setup

```bash
# One-time: create venv and install dependencies
make venv

# Or manually:
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt pytest
```

## Running the tests

```bash
make test

# Or manually (with venv active):
python3 -m pytest tests/ -v
```

Tests cover coordinate ordering, grid transforms, RF physics, solar budget logic, and the ILP optimizer — so porting to a new location will catch regressions immediately.

## Project structure

```
├── .github/workflows/
│   ├── tests.yml           CI unit tests
│   └── run.yml             Zero-host dispatch: inputs → main.py → artifacts + Pages deploy
├── viewer/
│   └── index.html          Static Leaflet map (OSM tiles, no build, no keys)
├── data/
│   ├── osm.py              OSM boundary, trees, buildings, paths (cached)
│   ├── elevation.py        USGS 3DEP elevation raster (cached)
│   ├── solar_irradiance.py NASA POWER monthly GHI + temperature (cached)
│   └── interference.py     Optional WiGLE RF interference layer
├── models/
│   ├── terrain.py          DSM construction, vectorised LOS ray-casting
│   ├── rf.py               ITU-R P.833 vegetation attenuation model
│   └── power.py            Solar harvest vs demand (panel/load/derating tunable)
├── optimize/
│   └── placer.py           HiGHS ILP set-cover optimizer
├── output/
│   └── writer.py           JSON, KML, and Markdown writers
├── tests/                  Unit tests (no network calls)
├── plans/                  Planning documents
└── main.py                 CLI entry point (all Actions inputs are flags here)
```

## Notes

- External data is cached in `data/cache/` after the first run — subsequent runs are fully offline. A baseline snapshot is committed so cloud runs never depend on live Overpass.
- **Cache freshness (lazy, no PR needed):** the Actions workflow checks the age of `data/cache/osm_trees.geojson` on every dispatch. If older than 90 days (or missing), that run re-fetches live via `--no-cache` and pushes the updated cache straight back to the same branch. Nothing updates unless somebody runs it — no cron, no PR. Pass `refresh-cache=true` to force a live re-fetch any time.
- November and December have insufficient sun for 24/7 operation at this latitude even with a fully exposed panel; the tool flags these as shoulder months in the summary
- The `--wigle-key` flag enables an optional RF interference layer from WiGLE.net (free account required)
