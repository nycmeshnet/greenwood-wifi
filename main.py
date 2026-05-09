"""Green-Wood Cemetery WiFi AP placement optimizer.

Usage
-----
    python main.py --range-2g 500 --range-5g 260
    python main.py --range-2g 500 --range-5g 260 --range-6g 200 --coverage paths
    python main.py --help
"""

import argparse
import os
import shutil
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
from pyproj import Transformer
from scipy.spatial import cKDTree
from shapely.geometry import Point
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
from models.power import daily_harvest_wh, solar_status, DEMAND_WH_PER_DAY
from optimize.placer import optimize_placement
from output.writer import write_placement_json, write_summary, write_kml
from constants import (
    FT_PER_M,
    CANDIDATE_SPACING_M, TEST_SPACING_M, PATH_BUFFER_M,
    PANEL_FACING, PANEL_AZIMUTH_DEG, PANEL_TILT_DEG,
)


# ---------------------------------------------------------------------------
# Coverage matrix worker (called from ThreadPoolExecutor)
# ---------------------------------------------------------------------------

def _candidate_coverage(
    i, ap_lat, ap_lon, nearby_idx, test_lats, test_lons,
    test_utm, ap_utm, bands_m, terrain,
):
    """Compute which test points are covered by a single AP candidate.

    Designed to be called from multiple threads concurrently — batch_los
    calls scipy.ndimage which releases the GIL, giving true parallelism.
    """
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

    req = parser.add_argument_group("required arguments")
    req.add_argument(
        "--range-2g", type=float, required=True, metavar="FT",
        help="Rated range of the AP on 2.4 GHz in feet",
    )
    req.add_argument(
        "--range-5g", type=float, required=True, metavar="FT",
        help="Rated range of the AP on 5 GHz in feet",
    )

    parser.add_argument(
        "--range-6g", type=float, default=None, metavar="FT",
        help=(
            "Rated range of the AP on 6 GHz in feet. "
            "Omit this flag entirely if the AP does not support 6 GHz."
        ),
    )
    parser.add_argument(
        "--coverage", choices=["full", "paths"], default="full",
        help=(
            "Coverage target for the optimiser. "
            "'full' — every point inside the cemetery boundary (default). "
            "'paths' — only areas within 15 ft of walkable paths."
        ),
    )
    parser.add_argument(
        "--wigle-key", default=None, metavar="KEY",
        help=(
            "WiGLE.net API key to layer in existing RF interference data. "
            "Optional — omit to skip interference modelling."
        ),
    )
    parser.add_argument(
        "--no-cache", action="store_true",
        help="Re-download all external data even if a local cache exists.",
    )
    return parser


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def main():
    parser = build_parser()
    args = parser.parse_args()

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

    print(f"\n=== Green-Wood WiFi Placement Optimizer ===")
    print(f"Bands: {[(f'{f} GHz', f'{r:.0f} ft') for f, r in bands_ft]}")
    print(f"Coverage mode: {args.coverage}\n")

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
    design_month, design_ghi = get_design_month(solar_data)
    off_season = shoulder_months(solar_data)
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

    # ------------------------------------------------------------------
    # 3. Generate candidate and test-point grids
    # ------------------------------------------------------------------
    print("\n=== Generating Grids ===")
    candidates = generate_grid(boundary, CANDIDATE_SPACING_M)

    if args.coverage == "paths":
        test_points = generate_path_grid(paths, boundary, TEST_SPACING_M)
    else:
        test_points = generate_grid(boundary, TEST_SPACING_M)

    print(f"Candidates: {len(candidates)},  Test points: {len(test_points)}")

    # ------------------------------------------------------------------
    # 4. Solar viability per candidate
    # ------------------------------------------------------------------
    print("\n=== Computing Solar Viability ===")
    t_solar = time.time()
    candidate_meta = []
    for lat, lon in candidates:
        shade = terrain.shade_fraction(lat, lon)
        harvest = daily_harvest_wh(design_ghi, shade)
        status = solar_status(harvest)
        candidate_meta.append({"shade": shade, "harvest": harvest, "status": status})

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
    # 5. Build coverage matrix  (parallelised over candidates)
    # ------------------------------------------------------------------
    print("\n=== Computing Coverage Matrix ===")
    t_cov = time.time()

    test_lats = np.array([p[0] for p in test_points])
    test_lons = np.array([p[1] for p in test_points])
    test_utm = np.column_stack(to_utm(test_lons, test_lats))
    test_kdtree = cKDTree(test_utm)

    # Pre-compute AP UTM coords and nearby test-point indices for all candidates.
    ap_lats = np.array([p[0] for p in viable_candidates])
    ap_lons = np.array([p[1] for p in viable_candidates])
    ap_utms = np.column_stack(to_utm(ap_lons, ap_lats))

    # query_ball_point with an array of query points is vectorised.
    all_nearby = test_kdtree.query_ball_point(ap_utms, primary_range_m * 1.05)

    coverage = np.zeros((len(viable_candidates), len(test_points)), dtype=bool)

    n_workers = min(os.cpu_count() or 4, 16)
    print(f"  {len(viable_candidates)} candidates × {len(test_points)} test points"
          f"  ({n_workers} threads)")

    done_count = 0
    with ThreadPoolExecutor(max_workers=n_workers) as pool:
        futures = {
            pool.submit(
                _candidate_coverage,
                i, ap_lats[i], ap_lons[i],
                np.array(all_nearby[i], dtype=int) if all_nearby[i] else np.array([], dtype=int),
                test_lats, test_lons, test_utm, ap_utms[i],
                bands_m, terrain,
            ): i
            for i in range(len(viable_candidates))
        }

        for future in as_completed(futures):
            i, nearby_idx, covered = future.result()
            if len(nearby_idx):
                coverage[i, nearby_idx] = covered
            done_count += 1
            if done_count % 200 == 0 or done_count == len(viable_candidates):
                elapsed = time.time() - t_cov
                rate = done_count / elapsed
                eta = (len(viable_candidates) - done_count) / rate if rate > 0 else 0
                print(
                    f"  {done_count}/{len(viable_candidates)} candidates  "
                    f"({elapsed:.0f}s elapsed, ~{eta:.0f}s remaining)    ",
                    end="\r",
                )

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
    # 7. Annotate selected APs and compute stats
    # ------------------------------------------------------------------
    selected_coverage = coverage[selected_idx]   # bool[n_sel, n_test]

    aps = []
    for rank, idx in enumerate(selected_idx):
        ap_lat, ap_lon = viable_candidates[idx]
        name = f"GW-AP-{rank + 1:02d}"
        meta = viable_meta[idx]

        ap_cov = coverage[idx]                   # bool[n_test]
        n_covered = int(ap_cov.sum())

        # Overlap: how many of this AP's covered points are also covered by another selected AP
        others = [j for j in selected_idx if j != idx]
        if others:
            other_cov = coverage[others].any(axis=0)
            overlap = int((ap_cov & other_cov).sum())
        else:
            overlap = 0
        overlap_pct = 100.0 * overlap / n_covered if n_covered else 0.0

        # Efficiency: covered area vs theoretical open-space circle
        rated_circle_area_ft2 = 3.14159 * primary_range_ft ** 2
        covered_area_ft2 = n_covered * (TEST_SPACING_M * FT_PER_M) ** 2
        efficiency_pct = min(100.0, 100.0 * covered_area_ft2 / rated_circle_area_ft2)

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
            "panel_facing": PANEL_FACING,
            "panel_azimuth_deg": PANEL_AZIMUTH_DEG,
            "panel_tilt_deg": PANEL_TILT_DEG,
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
        len(test_points),
        primary_range_ft,
        "output/summary.md",
        shoulder_months_list=off_season,
    )

    total_covered = int(selected_coverage.any(axis=0).sum())
    print(
        f"\nDone! {len(aps)} APs placed, "
        f"{total_covered:,}/{len(test_points):,} test points covered "
        f"({100*total_covered/len(test_points):.1f}%)."
    )
    print("  output/ap_placement.json")
    print("  output/ap_placement.kml  ← open in Google Earth")
    print("  output/summary.md")


if __name__ == "__main__":
    main()
