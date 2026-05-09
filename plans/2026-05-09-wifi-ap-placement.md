# Green-Wood Cemetery WiFi AP Placement Optimizer

## Site: Green-Wood Cemetery, Brooklyn NY
Confirmed boundary corners (WGS84 decimal degrees):
| Corner | Lat | Lon |
|---|---|---|
| NE | 40.659194 | -73.988389 |
| N  | 40.659556 | -73.995194 |
| NW | 40.652944 | -74.002056 |
| S  | 40.644250 | -73.989083 |
| SE | 40.647750 | -73.980500 |
| E  | 40.655278 | -73.981917 |

Bounding box: S=40.6443, W=-74.0021, N=40.6596, E=-73.9805

## Context
Build a CLI tool that takes per-band AP range as input and outputs:
1. `output/ap_placement.json` — optimal AP locations (lat, lon, name)
2. `output/summary.md` — AP count, flagged APs, suggestions

All data sources are free/no-key. All computation runs locally. Coverage mode is a runtime flag (default: full polygon coverage). Previous session scaffolded `data/osm.py` and dependency list but got no further.

---

## Data Sources (all free, all cached locally after first fetch)

| Layer | API | Key | Resolution | Python |
|---|---|---|---|---|
| Cemetery boundary, trees, buildings, paths | OpenStreetMap / Overpass | None | Individual objects | `overpy` ✅ exists |
| Elevation | USGS 3DEP (py3dep → National Map) | None | 1 m LiDAR for NYC | `py3dep` |
| Solar irradiance (monthly) | Open-Meteo | None | ~1 km, historical monthly | `requests` |
| Tree canopy height | OSM `height` tag + `diameter_crown` fallback | None | Per-tree | already in osm.py |
| RF interference density | WiGLE.net API | Free account | Crowdsourced | `requests` (optional flag) |
| Freeze-days / temperature | Open-Meteo historical | None | Hourly | `requests` |

---

## Variables modeled

- **FSPL** — free-space path loss at each frequency band
- **Vegetation attenuation** — ITU-R P.833: ~12 dB/100 m at 2.4 GHz, ~18 dB at 5 GHz, ~22 dB at 6 GHz, scaled by crown overlap along LOS path
- **Terrain obstruction** — line-of-sight check on digital surface model (elevation + tree heights)
- **Building blocking** — hard block if LOS crosses OSM building footprint
- **Solar shading** — tree canopy fraction above each candidate location reduces panel yield
- **Solar energy budget** — worst-month harvest (Open-Meteo irradiance × shade × 0.8 derating) vs 240 Wh/day demand (10 W × 24 h)
- **Interference** — WiGLE AP density as a soft modifier (optional)

---

## File Structure

```
solar_suggestions/
├── data/
│   ├── osm.py              EXISTS — boundary, trees, buildings
│   ├── elevation.py        NEW — py3dep raster over cemetery bbox
│   ├── solar_irradiance.py NEW — Open-Meteo monthly GHI for Brooklyn
│   └── interference.py     NEW — WiGLE AP density grid (optional)
├── models/
│   ├── terrain.py          NEW — DSM (elev + tree heights), LOS, diffraction
│   ├── rf.py               NEW — coverage polygon per candidate AP
│   └── power.py            NEW — solar viability per candidate location
├── optimize/
│   ├── __init__.py         NEW
│   └── placer.py           NEW — PuLP ILP set-cover
├── output/
│   └── writer.py           NEW — ap_placement.json + summary.md
├── main.py                 NEW — CLI entry point
└── requirements.txt        UPDATE — add py3dep, rasterio, pyproj
```

---

## Algorithm

### Step 1: Data fetch (cached to `data/cache/`)
- OSM: boundary polygon, tree points (with crown radius + height), building polys, path polylines
- 3DEP: elevation raster at 1 m resolution over cemetery bbox
- Open-Meteo: monthly average GHI (global horizontal irradiance) for Brooklyn (40.65°N, -73.99°W)

### Step 2: Build Digital Surface Model
- Start with bare-earth elevation raster
- Add tree height (OSM `height` tag; default 15 m if missing) at each tree crown centroid, blended over crown radius
- Result: DSM array used for LOS checks

### Step 3: Generate candidate grid
- UTM zone 18N (EPSG:32618) projection for metric distances
- 20 m grid of points clipped to cemetery boundary polygon
- ~1,500–2,500 candidate AP locations

### Step 4: Generate test-point grid
- 10 m grid of points clipped to cemetery boundary
- Coverage mode flag:
  - `full` — all grid points must be covered (default)
  - `paths` — only points within 15 m of OSM path polylines

### Step 5: Per-candidate solar viability
- Pull Open-Meteo temperature history for Brooklyn to determine above-freezing operating window (~March–November)
- Design month = lowest-GHI month within that window (typically November, ~3.0 PSH for NYC)
- For each candidate, compute shade factor from OSM tree crowns overhead
- Daily harvest = GHI_design_month × (1 − shade) × 100 W × 0.80 derating
- Threshold: 240 Wh/day (10 W × 24 h)
- Clearly unviable (harvest < 80% of threshold): exclude from candidates
- Marginal (harvest 80–110% of threshold): kept but given a cost penalty in ILP so optimizer prefers non-marginal alternatives; flagged by name in summary

### Step 6: Per-candidate coverage matrix
For each viable candidate AP at each input frequency band:
1. Cast rays to every test point
2. LOS check: clear, tree-blocked, or building-blocked
3. If clear: apply FSPL only; clip at input range
4. If tree-blocked: apply ITU-R P.833 vegetation loss; reduce effective range
5. If building-blocked: mark as not covered
6. Coverage matrix `C[candidate, test_point]` = 1 if test point is covered

### Step 7: ILP optimization (PuLP)
```
minimize  Σ x_i
subject to  Σ_i C[i,j] * x_i >= 1  for all test points j
            x_i ∈ {0, 1}
```
Uses CBC solver bundled with PuLP (no external solver needed).

### Step 8: Output
**`output/ap_placement.json`**
```json
[
  {"name": "GW-AP-01", "lat": 40.6512, "lon": -73.9941, "bands": ["2.4", "5"]},
  ...
]
```

**`output/summary.md`**
- Total AP count
- APs flagged as solar-marginal (by name)
- APs with underutilized range (cheaper AP possible)
- APs with high overlap with neighbors
- Uncovered point count (if any)

---

## Units
- **All user-facing inputs and outputs use US customary units** (feet, acres)
- Internal calculations use metric (meters) — feet are converted at CLI parse time and back at output time
- 1 ft = 0.3048 m (exact)

## CLI

```bash
python main.py \
  --range-2g 500 \       # feet, required
  --range-5g 260 \       # feet, required
  --range-6g 200 \       # feet, optional — omit if AP has no 6 GHz radio
  --coverage full \      # full | paths (default: full)
  --wigle-key KEY \      # optional, enables interference layer
  --no-cache             # re-fetch all data
```

### --help output (argparse help strings)

```
usage: main.py --range-2g FT --range-5g FT [--range-6g FT] [--coverage MODE] [options]

Outputs optimal solar-powered WiFi AP placement for Green-Wood Cemetery.

required arguments:
  --range-2g FT       Rated range of the AP on 2.4 GHz in feet

  --range-5g FT       Rated range of the AP on 5 GHz in feet

optional arguments:
  --range-6g FT       Rated range of the AP on 6 GHz in feet.
                      Omit this flag entirely if the AP does not support 6 GHz.

  --coverage MODE     Coverage target for optimization. Choices:
                        full   — every point inside the cemetery boundary (default)
                        paths  — only areas within 15 ft of walkable paths
                      (more modes can be added; 'full' is recommended for initial sizing)

  --wigle-key KEY     WiGLE.net API key to layer in existing RF interference data.
                      Optional — omit to skip interference modeling.

  --no-cache          Re-download all external data even if a local cache exists.

  -h, --help          Show this message and exit.

outputs:
  output/ap_placement.json   Lat/lon + name for each recommended AP
  output/summary.md          AP count, solar-marginal sites, range suggestions (distances in ft)
```

---

## New dependencies to add
- `py3dep` — USGS 3DEP elevation via National Map
- `rasterio` — raster elevation data handling
- `pyproj` — UTM projection for metric distance calculations

---

## Verification
1. Run `python main.py --range-2g 150 --range-5g 80` — fetches data, solves ILP, writes outputs
2. Check `output/ap_placement.json` — lat/lon within cemetery bbox
3. Check `output/summary.md` — AP count, flagged entries present
4. Spot-check 2-3 AP locations on satellite imagery for plausibility
5. Run with `--range-2g 300` — AP count should drop significantly
