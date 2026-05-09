# Green-Wood Cemetery WiFi AP Placement Optimizer

Calculates the minimum number of solar-powered WiFi access points needed to cover Green-Wood Cemetery in Brooklyn, NY, and outputs their optimal locations.

## What it does

Given the rated range of an access point on each frequency band, the tool:

1. Fetches cemetery topology, tree cover, and building footprints from OpenStreetMap
2. Downloads a 5 m elevation raster from USGS 3DEP
3. Pulls multi-year monthly solar irradiance from NASA POWER
4. Models RF propagation using ITU-R P.833 vegetation attenuation
5. Checks solar viability for each candidate location (100 W panel, 10 W load, 50 Wh battery)
6. Solves an Integer Linear Program (PuLP/CBC) to minimize AP count
7. Writes results as JSON, a Google Earth KML overlay, and a Markdown summary

**No API keys required.** All data sources are free and open.

## Hardware assumptions (per AP node)

| Parameter | Value |
|---|---|
| Solar panel | 100 W rated |
| Load | 10 W continuous |
| Battery | 50 Wh |
| Derating factor | 0.80 (temperature, wiring, soiling) |
| Design basis | Worst above-freezing month with sufficient sun (~October for Brooklyn) |
| Thermal cutoff | Below ~34 °F average monthly temperature |

## Usage

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

# Re-fetch all external data
python3 main.py --no-cache
```

All range values are in **feet**.

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
├── data/
│   ├── osm.py              OSM boundary, trees, buildings, paths (cached)
│   ├── elevation.py        USGS 3DEP elevation raster (cached)
│   ├── solar_irradiance.py NASA POWER monthly GHI + temperature (cached)
│   └── interference.py     Optional WiGLE RF interference layer
├── models/
│   ├── terrain.py          DSM construction, vectorised LOS ray-casting
│   ├── rf.py               ITU-R P.833 vegetation attenuation model
│   └── power.py            Solar harvest vs 240 Wh/day demand
├── optimize/
│   └── placer.py           PuLP ILP set-cover optimizer
├── output/
│   └── writer.py           JSON, KML, and Markdown writers
├── tests/                  Unit tests (no network calls)
├── plans/                  Planning documents
└── main.py                 CLI entry point
```

## Notes

- External data is cached in `data/cache/` after the first run — subsequent runs are fully offline
- November and December have insufficient sun for 24/7 operation at this latitude even with a fully exposed panel; the tool flags these as shoulder months in the summary
- The `--wigle-key` flag enables an optional RF interference layer from WiGLE.net (free account required)
