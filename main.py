"""Green-Wood Cemetery WiFi AP placement optimizer.

Usage
-----
    python main.py                                   # uses built-in defaults
    python main.py --range-2g 2000 --range-5g 1250
    python main.py --range-2g 2000 --range-5g 1250 --range-6g 800 --coverage paths
    python main.py --help
"""

import argparse
import multiprocessing as mp
import os
import shutil
import time
from concurrent.futures import ProcessPoolExecutor, as_completed

import numpy as np
from pyproj import Transformer
from scipy.spatial import cKDTree
from shapely.geometry import Point, Polygon
from shapely.ops import transform as shapely_transform

from data.osm import (
    fetch_cemetery_boundary,
    fetch_trees,
    fetch_buildings,
    fetch_paths,
)
from data.elevation import fetch_elevation_raster
from data.solar_irradiance import (
    fetch_solar_and_temperature,
    get_design_month,
    shoulder_months,
    MONTH_NAMES,
)
from data.interference import fetch_wigle_density
from models.terrain import TerrainModel, to_utm, to_wgs84
from models.rf import effective_range_m
from models.power import (
    daily_harvest_wh, solar_status, DEMAND_WH_PER_DAY,
    best_panel_azimuth, facing_label,
)
from optimize.placer import optimize_placement
from output.writer import write_placement_json, write_summary, write_kml
import constants
import data.osm as osm_module
import data.solar_irradiance as solar_module
import models.power as power_module
import models.terrain as terrain_module
import models.rf as rf_module
import optimize.placer as placer_module
from constants import (
    FT_PER_M,
    CANDIDATE_SPACING_M, TEST_SPACING_M, ILP_TEST_SPACING_M, PATH_BUFFER_M,
    PANEL_TILT_DEG, DEFAULT_TREE_HEIGHT_M,
)


# ---------------------------------------------------------------------------
# Coverage matrix — fork-based process pool with shared state
# ---------------------------------------------------------------------------
# These are set in main() before the ProcessPoolExecutor is created.
# fork() copies the parent's address space (copy-on-write), so child
# processes inherit these without any serialisation overhead.
_mp_terrain = None
_mp_ilp_lats = _mp_ilp_lons = _mp_ilp_utm = None
_mp_ap_lats = _mp_ap_lons = _mp_ap_utms = None
_mp_all_nearby = _mp_bands_m = None


def _coverage_chunk(start_i: int, end_i: int):
    """Process candidates[start_i:end_i] using fork-inherited globals."""
    results = []
    for i in range(start_i, end_i):
        nearby_raw = _mp_all_nearby[i]
        if not nearby_raw:
            results.append((i, np.array([], dtype=int), np.array([], dtype=bool)))
            continue
        nearby_idx = np.array(nearby_raw, dtype=int)
        ap_utm = _mp_ap_utms[i]

        nl = _mp_ilp_lats[nearby_idx]
        no = _mp_ilp_lons[nearby_idx]
        los_clear, veg_path = _mp_terrain.batch_los(
            _mp_ap_lats[i], _mp_ap_lons[i], nl, no
        )
        distances_m = np.linalg.norm(_mp_ilp_utm[nearby_idx] - ap_utm, axis=1)

        covered = np.zeros(len(nearby_idx), dtype=bool)
        for freq, rated_m in _mp_bands_m:
            eff_range = effective_range_m(rated_m, freq, veg_path)
            covered |= los_clear & (distances_m <= eff_range)

        results.append((i, nearby_idx, covered))
    return results


def _candidate_coverage(
    i, ap_lat, ap_lon, nearby_idx, test_lats, test_lons,
    test_utm, ap_utm, bands_m, terrain,
):
    """Single-candidate helper used for the post-ILP fine-grid pass."""
    if len(nearby_idx) == 0:
        return i, nearby_idx, np.array([], dtype=bool)
    nl = test_lats[nearby_idx]
    no = test_lons[nearby_idx]
    los_clear, veg_path = terrain.batch_los(ap_lat, ap_lon, nl, no)
    distances_m = np.linalg.norm(test_utm[nearby_idx] - ap_utm, axis=1)
    covered = np.zeros(len(nearby_idx), dtype=bool)
    for freq, rated_m in bands_m:
        eff_range = effective_range_m(rated_m, freq, veg_path)
        covered |= los_clear & (distances_m <= eff_range)
    return i, nearby_idx, covered


# ---------------------------------------------------------------------------
# Grid helpers
# ---------------------------------------------------------------------------

def _build_utm_projector(boundary_wgs84):
    fwd = Transformer.from_crs("EPSG:4326", "EPSG:32618", always_xy=True)
    return shapely_transform(fwd.transform, boundary_wgs84)


def generate_grid(boundary_wgs84, spacing_m: float) -> list[tuple[float, float]]:
    """Return (lat, lon) tuples on a metric grid clipped to the WGS84 boundary."""
    fwd = Transformer.from_crs("EPSG:4326", "EPSG:32618", always_xy=True)
    inv = Transformer.from_crs("EPSG:32618", "EPSG:4326", always_xy=True)

    boundary_utm = shapely_transform(fwd.transform, boundary_wgs84)
    minx, miny, maxx, maxy = boundary_utm.bounds

    xs = np.arange(minx, maxx, spacing_m)
    ys = np.arange(miny, maxy, spacing_m)

    points = []
    for x in xs:
        for y in ys:
            lon, lat = inv.transform(x, y)
            if boundary_wgs84.contains(Point(lon, lat)):
                points.append((lat, lon))
    return points


def generate_path_grid(paths_gdf, boundary_wgs84, spacing_m: float) -> list[tuple[float, float]]:
    """Return grid points within PATH_BUFFER_M of any path, inside the boundary."""
    if paths_gdf.empty:
        return generate_grid(boundary_wgs84, spacing_m)

    buffered = paths_gdf.geometry.buffer(PATH_BUFFER_M / 111_320).unary_union
    buffered = buffered.intersection(boundary_wgs84)

    fwd = Transformer.from_crs("EPSG:4326", "EPSG:32618", always_xy=True)
    inv = Transformer.from_crs("EPSG:32618", "EPSG:4326", always_xy=True)

    boundary_utm = shapely_transform(fwd.transform, boundary_wgs84)
    minx, miny, maxx, maxy = boundary_utm.bounds
    xs = np.arange(minx, maxx, spacing_m)
    ys = np.arange(miny, maxy, spacing_m)

    points = []
    for x in xs:
        for y in ys:
            lon, lat = inv.transform(x, y)
            pt = Point(lon, lat)
            if buffered.contains(pt):
                points.append((lat, lon))
    return points


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="Outputs optimal solar-powered WiFi AP placement for Green-Wood Cemetery.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
outputs:
  output/ap_placement.json   Machine-readable AP list (lat/lon/name)
  output/ap_placement.kml    Google Earth overlay — open directly in Google Earth
  output/summary.md          AP count, solar-marginal sites, range suggestions
        """,
    )

    parser.add_argument(
        "--range-2g", type=float, default=2000.0, metavar="FT",
        help="AP open-field rated range on 2.4 GHz, feet (default: 2000). Trees shrink this via ITU-R P.833. Larger = fewer APs.",
    )
    parser.add_argument(
        "--range-5g", type=float, default=1250.0, metavar="FT",
        help="AP open-field rated range on 5 GHz, feet (default: 1250). Higher bands attenuate faster in canopy.",
    )

    parser.add_argument(
        "--range-6g", type=float, default=None, metavar="FT",
        help=(
            "AP open-field rated range on 6 GHz, feet. "
            "Omit entirely if the AP is not tri-band."
        ),
    )
    parser.add_argument(
        "--coverage", choices=["full", "paths"], default="full",
        help=(
            "'full' = cover every point inside the boundary (default). "
            "'paths' = cover only ground within 15 ft of walkable paths (faster, fewer APs)."
        ),
    )
    parser.add_argument(
        "--wigle-key", default=None, metavar="KEY",
        help=(
            "Optional WiGLE.net API key to overlay existing RF interference. "
            "Omit to skip (free account required if used)."
        ),
    )
    parser.add_argument(
        "--no-cache", action="store_true",
        help="Ignore data/cache/ and re-download OSM, USGS elevation, and NASA solar.",
    )
    # --- Site ---
    parser.add_argument(
        "--relation-id", type=int, default=1370699, metavar="ID",
        help="OpenStreetMap relation ID defining the site boundary (default 1370699 = Green-Wood). Change to port to a new site.",
    )
    # --- Hardware / solar ---
    parser.add_argument("--panel-w", type=float, default=100.0, metavar="W", help="Solar panel nameplate watts (default: 100). Harvest = GHI/1000 x panel x (1-shade) x derating.")
    parser.add_argument("--load-w", type=float, default=10.0, metavar="W", help="AP continuous draw in watts (default: 10). Daily demand = load x 24h (default 240 Wh/day).")
    parser.add_argument("--derating", type=float, default=0.80, metavar="FACTOR", help="Temperature/wiring/soiling losses as 0-1 factor (default: 0.80).")
    parser.add_argument("--tilt", type=float, default=40.0, metavar="DEG", help="Panel tilt from horizontal in degrees, written to outputs (default: 40, ~site latitude). Azimuth is auto-optimized per site.")
    parser.add_argument(
        "--freeze-thresh-f", type=float, default=34.0, metavar="F",
        help="Months averaging below this temp are excluded — batteries can't run through freezes (default: 34 F, above 32 to allow for nightly freezes).",
    )
    # --- Grid / solver (advanced: auto-scaled, leave alone unless the solve is too slow) ---
    parser.add_argument("--candidate-spacing", type=float, default=20.0, metavar="M", help="[advanced] Grid of possible AP spots in metres (default: 20). Effective spacing = max(this, range/10). Smaller = better placement, ~4x more work.")
    parser.add_argument("--ilp-spacing", type=float, default=20.0, metavar="M", help="[advanced] Grid the optimizer must cover in metres (default: 20). Effective spacing = max(this, range/10). Smaller = tighter guarantees, much slower HiGHS solve.")
    parser.add_argument("--test-spacing", type=float, default=10.0, metavar="M", help="[advanced] Fine grid for reporting only in metres (default: 10). Does not affect the solve.")
    parser.add_argument(
        "--mount-height-m", "--ap-height", dest="ap_height", type=float, default=2.0, metavar="M",
        help="AP pole mount height above ground in metres (default: 2.0). --ap-height is a deprecated alias.",
    )
    parser.add_argument(
        "--client-height-m", "--rx-height", dest="rx_height", type=float, default=2.0, metavar="M",
        help="Client device height above ground in metres, e.g. phone in hand (default: 2.0). --rx-height is a deprecated alias.",
    )
    parser.add_argument("--shade-radius", type=float, default=50.0, metavar="M", help="[advanced] Trees within this radius count toward shade (default: 50). Pass 0 to auto-compute as max(30, 3x tree height).")
    parser.add_argument(
        "--marginal-penalty", type=float, default=1.5, metavar="X",
        help="[advanced] Policy knob: ILP cost multiplier for shade-marginal spots (default: 1.5). 1.0 = treat equal to viable, 2.0 = strongly avoid even if it adds APs.",
    )
    return parser


def apply_overrides(args) -> dict:
    """Patch constants + already-imported module globals so CLI/Actions inputs take effect.

    Returns dict of effective values for logging. Must be called before any
    pipeline work (models use `from constants import X` bindings evaluated at
    import time, so we patch each consumer module explicitly).
    """
    eff = {}
    # 0 = auto: shadow length ~= tree height / tan(sun elev); ~3x height covers
    # low shoulder-season sun. Floor at 30 m so short trees still see neighbours.
    shade_radius = args.shade_radius
    if shade_radius <= 0:
        shade_radius = max(30.0, 3.0 * DEFAULT_TREE_HEIGHT_M)
    # constants module (source of truth for fresh imports)
    constants.CEMETERY_RELATION_ID = args.relation_id
    constants.PANEL_RATED_W = args.panel_w
    constants.LOAD_W = args.load_w
    constants.DERATING = args.derating
    constants.DEMAND_WH_PER_DAY = args.load_w * 24.0
    constants.PANEL_TILT_DEG = args.tilt
    constants.FREEZE_THRESHOLD_F = args.freeze_thresh_f
    constants.CANDIDATE_SPACING_M = args.candidate_spacing
    constants.ILP_TEST_SPACING_M = args.ilp_spacing
    constants.TEST_SPACING_M = args.test_spacing
    constants.AP_HEIGHT_M = args.ap_height
    constants.RX_HEIGHT_M = args.rx_height
    constants.SHADE_RADIUS_M = shade_radius
    constants.MARGINAL_PENALTY = args.marginal_penalty

    # data.osm (bound `from constants import` at import time)
    osm_module.CEMETERY_RELATION_ID = args.relation_id

    # models.power
    power_module.PANEL_RATED_W = args.panel_w
    power_module.LOAD_W = args.load_w
    power_module.DERATING = args.derating
    power_module.DEMAND_WH_PER_DAY = args.load_w * 24.0

    # models.terrain (heights read at call time; radius defaults bound at def time
    # so main.py passes radius explicitly — patch here as well for direct callers)
    terrain_module.AP_HEIGHT_M = args.ap_height
    terrain_module.RX_HEIGHT_M = args.rx_height
    terrain_module.SHADE_RADIUS_M = shade_radius
    for fn_name in ("shade_by_azimuth", "shade_fraction"):
        fn = getattr(terrain_module.TerrainModel, fn_name, None)
        if fn is not None and fn.__defaults__:
            # last default is radius_m for both functions
            defaults = list(fn.__defaults__)
            defaults[-1] = shade_radius
            fn.__defaults__ = tuple(defaults)

    # optimize.placer
    placer_module.MARGINAL_PENALTY = args.marginal_penalty

    eff = {
        "relation_id": args.relation_id,
        "panel_w": args.panel_w,
        "load_w": args.load_w,
        "demand_wh": args.load_w * 24.0,
        "derating": args.derating,
        "tilt": args.tilt,
        "freeze_thresh_f": args.freeze_thresh_f,
        "candidate_spacing_m": args.candidate_spacing,
        "ilp_spacing_m": args.ilp_spacing,
        "test_spacing_m": args.test_spacing,
        "shade_radius_m": shade_radius,
        "ap_height_m": args.ap_height,
        "rx_height_m": args.rx_height,
    }
    return eff


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def main():
    parser = build_parser()
    args = parser.parse_args()
    eff = apply_overrides(args)
    CAND_SPACING = eff["candidate_spacing_m"]
    ILP_SPACING_BASE = args.ilp_spacing
    TEST_SPACING = eff["test_spacing_m"]

    if args.no_cache:
        cache_dir = os.path.join("data", "cache")
        if os.path.isdir(cache_dir):
            shutil.rmtree(cache_dir)
            print("Cache cleared.")

    # Convert user-supplied feet to metres for all internal calculations
    bands_ft = [(2.4, args.range_2g), (5.0, args.range_5g)]
    if args.range_6g:
        bands_ft.append((6.0, args.range_6g))
    bands_m = [(freq, rng / FT_PER_M) for freq, rng in bands_ft]
    primary_freq, primary_range_m = bands_m[0]   # 2.4 GHz — longest range
    primary_range_ft = primary_range_m * FT_PER_M

    # At large ranges the constraint matrix becomes dense (density ≈ π·r²/area).
    # Increasing grid spacing reduces problem size as spacing², keeping IPM tractable.
    adaptive_cand_m = max(CAND_SPACING, primary_range_m / 10)
    adaptive_ilp_m  = max(ILP_SPACING_BASE,  primary_range_m / 10)

    print(f"\n=== Green-Wood WiFi Placement Optimizer ===")
    print(f"Bands: {[(f'{f} GHz', f'{r:.0f} ft') for f, r in bands_ft]}")
    print(f"Coverage mode: {args.coverage}")
    print(f"Site relation: {eff['relation_id']}  "
          f"Panel: {eff['panel_w']:.0f}W load {eff['load_w']:.1f}W "
          f"derate {eff['derating']:.2f} tilt {eff['tilt']:.0f}°  "
          f"mount {eff['ap_height_m']:.1f}m client {eff['rx_height_m']:.1f}m  "
          f"shade-r {eff['shade_radius_m']:.0f}m\n")

    # ------------------------------------------------------------------
    # 1. Fetch data
    # ------------------------------------------------------------------
    print("=== Fetching OSM Data ===")
    nc = args.no_cache
    boundary = fetch_cemetery_boundary(no_cache=nc)
    trees = fetch_trees(boundary, no_cache=nc)
    buildings = fetch_buildings(boundary, no_cache=nc)
    paths = fetch_paths(boundary, no_cache=nc)

    print("\n=== Fetching Elevation ===")
    elevation, elev_transform = fetch_elevation_raster(boundary, no_cache=args.no_cache)

    print("\n=== Fetching Solar Data ===")
    solar_data = fetch_solar_and_temperature(no_cache=args.no_cache)
    design_month, design_ghi = get_design_month(
        solar_data,
        freeze_threshold_f=args.freeze_thresh_f,
        panel_w=args.panel_w,
        load_w=args.load_w,
        derating=args.derating,
    )
    off_season = shoulder_months(
        solar_data,
        freeze_threshold_f=args.freeze_thresh_f,
        panel_w=args.panel_w,
        load_w=args.load_w,
        derating=args.derating,
    )
    print(
        f"Design month: {MONTH_NAMES[design_month]} "
        f"(GHI {design_ghi:.0f} Wh/m²/day, "
        f"avg temp {solar_data[str(design_month)]['avg_temp_f']:.1f} °F)"
    )
    if off_season:
        print(f"Shoulder months (limited solar, daytime-only): {', '.join(off_season)}")

    # ------------------------------------------------------------------
    # 2. Build terrain model
    # ------------------------------------------------------------------
    print("\n=== Building Terrain Model ===")
    terrain = TerrainModel(elevation, elev_transform, trees, buildings)

    # Project boundary to UTM once for circle/polygon intersection downstream
    # (used to fairly score coverage efficiency for APs near the perimeter).
    boundary_utm = Polygon(
        [to_utm(lon, lat) for lon, lat in boundary.exterior.coords],
        holes=[
            [to_utm(lon, lat) for lon, lat in interior.coords]
            for interior in boundary.interiors
        ],
    )

    # ------------------------------------------------------------------
    # 3. Generate candidate and test-point grids
    # ------------------------------------------------------------------
    print("\n=== Generating Grids ===")
    candidates = generate_grid(boundary, adaptive_cand_m)

    if args.coverage == "paths":
        report_points = generate_path_grid(paths, boundary, TEST_SPACING)
        ilp_points    = generate_path_grid(paths, boundary, adaptive_ilp_m)
    else:
        report_points = generate_grid(boundary, TEST_SPACING)
        ilp_points    = generate_grid(boundary, adaptive_ilp_m)

    print(f"Grid spacing : candidates {adaptive_cand_m:.0f} m  "
          f"ILP {adaptive_ilp_m:.0f} m  report {TEST_SPACING} m")
    print(f"Candidates: {len(candidates):,}  "
          f"ILP test grid: {len(ilp_points):,}  "
          f"Report grid: {len(report_points):,}")

    # ------------------------------------------------------------------
    # 4. Solar viability per candidate
    # ------------------------------------------------------------------
    print("\n=== Computing Solar Viability ===")
    t_solar = time.time()
    candidate_meta = []
    for lat, lon in candidates:
        dir_shade = terrain.shade_by_azimuth(lat, lon, radius_m=eff["shade_radius_m"])
        shade = float(min(dir_shade.sum(), 1.0))
        harvest = daily_harvest_wh(design_ghi, shade)
        status = solar_status(harvest)
        azimuth, sky_score = best_panel_azimuth(dir_shade, latitude_deg=lat)
        candidate_meta.append({
            "shade": shade,
            "harvest": harvest,
            "status": status,
            "panel_azimuth_deg": round(azimuth, 1),
            "panel_sky_score": round(sky_score, 3),
        })

    viable_idx = [i for i, m in enumerate(candidate_meta) if m["status"] != "unviable"]
    viable_candidates = [candidates[i] for i in viable_idx]
    viable_meta = [candidate_meta[i] for i in viable_idx]

    n_unviable = len(candidates) - len(viable_candidates)
    n_marginal = sum(1 for m in viable_meta if m["status"] == "marginal")
    print(
        f"Viable: {len(viable_candidates) - n_marginal}  "
        f"Marginal: {n_marginal}  "
        f"Unviable (too shaded): {n_unviable}  "
        f"({time.time()-t_solar:.1f}s)"
    )

    if not viable_candidates:
        print("ERROR: No viable candidate locations found. "
              "Check that the cemetery has open-sky areas.")
        return

    # ------------------------------------------------------------------
    # 5. Build coverage matrix  (process pool, fork-inherited shared state)
    # ------------------------------------------------------------------
    print("\n=== Computing Coverage Matrix ===")
    t_cov = time.time()

    # ILP uses the coarser grid; fine grid is used only for coverage reporting.
    ilp_lats = np.array([p[0] for p in ilp_points])
    ilp_lons = np.array([p[1] for p in ilp_points])
    ilp_utm  = np.column_stack(to_utm(ilp_lons, ilp_lats))
    ilp_kdtree = cKDTree(ilp_utm)

    ap_lats = np.array([p[0] for p in viable_candidates])
    ap_lons = np.array([p[1] for p in viable_candidates])
    ap_utms = np.column_stack(to_utm(ap_lons, ap_lats))

    all_nearby = ilp_kdtree.query_ball_point(ap_utms, primary_range_m * 1.05)

    # Set module-level globals BEFORE forking so workers inherit them
    # without any serialisation cost (copy-on-write pages).
    global _mp_terrain, _mp_ilp_lats, _mp_ilp_lons, _mp_ilp_utm
    global _mp_ap_lats, _mp_ap_lons, _mp_ap_utms, _mp_all_nearby, _mp_bands_m
    _mp_terrain   = terrain
    _mp_ilp_lats  = ilp_lats
    _mp_ilp_lons  = ilp_lons
    _mp_ilp_utm   = ilp_utm
    _mp_ap_lats   = ap_lats
    _mp_ap_lons   = ap_lons
    _mp_ap_utms   = ap_utms
    _mp_all_nearby = all_nearby
    _mp_bands_m   = bands_m

    n_cands = len(viable_candidates)
    n_workers = os.cpu_count() or 4
    # Chunk candidates so each worker processes ~50 at a time, keeping all
    # cores busy without the overhead of one future per candidate.
    chunk_size = max(1, n_cands // (n_workers * 8))
    chunks = [(i, min(i + chunk_size, n_cands))
              for i in range(0, n_cands, chunk_size)]

    coverage = np.zeros((n_cands, len(ilp_points)), dtype=bool)

    print(f"  {n_cands:,} candidates × {len(ilp_points):,} ILP points  "
          f"({n_workers} processes, {len(chunks)} chunks of ~{chunk_size})")

    done_count = 0
    ctx = mp.get_context("fork")
    pool = ProcessPoolExecutor(max_workers=n_workers, mp_context=ctx)
    try:
        futures = {pool.submit(_coverage_chunk, s, e): (s, e) for s, e in chunks}
        for future in as_completed(futures):
            for i, nearby_idx, covered in future.result():
                if len(nearby_idx):
                    coverage[i, nearby_idx] = covered
                done_count += 1
            if done_count % max(chunk_size, 200) < chunk_size or done_count == n_cands:
                elapsed = time.time() - t_cov
                rate = done_count / elapsed if elapsed > 0 else 1
                eta = (n_cands - done_count) / rate
                print(
                    f"  {done_count:,}/{n_cands:,} candidates  "
                    f"({elapsed:.0f}s elapsed, ~{eta:.0f}s remaining)    ",
                    end="\r",
                )
    except KeyboardInterrupt:
        pool.shutdown(wait=False, cancel_futures=True)
        print("\nInterrupted.")
        raise SystemExit(1)
    else:
        pool.shutdown(wait=False)

    print(f"\n  Coverage matrix: {coverage.sum():,} covered pairs  "
          f"({time.time()-t_cov:.1f}s total)")

    # ------------------------------------------------------------------
    # 6. ILP optimisation
    # ------------------------------------------------------------------
    print("\n=== Running ILP Optimiser ===")
    t_ilp = time.time()
    statuses = [m["status"] for m in viable_meta]
    selected_idx = optimize_placement(coverage, statuses)

    if not selected_idx:
        print("No solution found — try increasing range values.")
        return

    # ------------------------------------------------------------------
    # 7. Fine-grid coverage for selected APs (stats + summary)
    # ------------------------------------------------------------------
    print("\n=== Computing Fine Coverage for Selected APs ===")
    rep_lats = np.array([p[0] for p in report_points])
    rep_lons = np.array([p[1] for p in report_points])
    rep_utm  = np.column_stack(to_utm(rep_lons, rep_lats))
    rep_kdtree = cKDTree(rep_utm)

    n_sel = len(selected_idx)
    sel_lats = ap_lats[selected_idx]
    sel_lons = ap_lons[selected_idx]
    sel_utms = ap_utms[selected_idx]
    sel_nearby = rep_kdtree.query_ball_point(sel_utms, primary_range_m * 1.05)

    selected_coverage = np.zeros((n_sel, len(report_points)), dtype=bool)
    for rank in range(n_sel):
        nearby_idx = np.array(sel_nearby[rank], dtype=int) if sel_nearby[rank] else np.array([], dtype=int)
        if len(nearby_idx):
            _, near, cov = _candidate_coverage(
                rank, sel_lats[rank], sel_lons[rank],
                nearby_idx, rep_lats, rep_lons, rep_utm, sel_utms[rank],
                bands_m, terrain,
            )
            selected_coverage[rank, near] = cov

    aps = []
    for rank, idx in enumerate(selected_idx):
        ap_lat, ap_lon = viable_candidates[idx]
        name = f"GW-AP-{rank + 1:02d}"
        meta = viable_meta[idx]

        ap_cov = selected_coverage[rank]
        n_covered = int(ap_cov.sum())

        # Overlap: how many of this AP's covered points are also covered by another selected AP
        others = [r for r in range(n_sel) if r != rank]
        if others:
            other_cov = selected_coverage[others].any(axis=0)
            overlap = int((ap_cov & other_cov).sum())
        else:
            overlap = 0
        overlap_pct = 100.0 * overlap / n_covered if n_covered else 0.0

        # Efficiency: covered area vs the *reachable* open-space circle, where
        # "reachable" = rated-range circle ∩ cemetery boundary. Clipping to the
        # boundary stops APs near the perimeter from looking artificially
        # underutilised when half their rated circle lies outside the site.
        ap_circle_utm = Point(*ap_utms[idx]).buffer(primary_range_m)
        rated_area_m2 = ap_circle_utm.intersection(boundary_utm).area
        rated_circle_area_ft2 = max(rated_area_m2 * (FT_PER_M ** 2), 1.0)
        covered_area_ft2 = n_covered * (TEST_SPACING * FT_PER_M) ** 2
        efficiency_pct = min(100.0, 100.0 * covered_area_ft2 / rated_circle_area_ft2)

        ap_azimuth = meta["panel_azimuth_deg"]
        aps.append({
            "name": name,
            "lat": round(ap_lat, 6),
            "lon": round(ap_lon, 6),
            "bands": [f"{f:.4g}GHz" for f, _ in bands_ft],
            "solar_status": meta["status"],
            "harvest_wh": round(meta["harvest"], 1),
            "shade_pct": round(meta["shade"] * 100.0, 1),
            "coverage_efficiency_pct": round(efficiency_pct, 1),
            "overlap_pct": round(overlap_pct, 1),
            "panel_facing": facing_label(ap_azimuth),
            "panel_azimuth_deg": ap_azimuth,
            "panel_tilt_deg": args.tilt,
            "panel_sky_score": meta["panel_sky_score"],
        })

    # ------------------------------------------------------------------
    # 8. Write output
    # ------------------------------------------------------------------
    print("\n=== Writing Output ===")
    os.makedirs("output", exist_ok=True)
    write_placement_json(aps, "output/ap_placement.json")
    write_kml(aps, "output/ap_placement.kml")
    write_summary(
        aps,
        selected_coverage,
        len(report_points),
        primary_range_ft,
        "output/summary.md",
        shoulder_months_list=off_season,
    )

    total_covered = int(selected_coverage.any(axis=0).sum())
    print(
        f"\nDone! {len(aps)} APs placed, "
        f"{total_covered:,}/{len(report_points):,} test points covered "
        f"({100*total_covered/len(report_points):.1f}%)."
    )
    print("  output/ap_placement.json")
    print("  output/ap_placement.kml  ← open in Google Earth")
    print("  output/summary.md")


if __name__ == "__main__":
    main()
