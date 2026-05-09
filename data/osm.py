"""Fetch Green-Wood Cemetery boundary, trees, buildings, and paths from OpenStreetMap.

All results are cached to data/cache/ as GeoJSON/JSON so Overpass is only
hit on the first run (or when --no-cache is passed).  No API keys required.
"""

import json
import os
import time

import geopandas as gpd
import overpy
from shapely.geometry import LineString, Point, Polygon
from shapely.ops import unary_union

CACHE_DIR = os.path.join(os.path.dirname(__file__), "cache")

# Green-Wood Cemetery, Brooklyn NY — OSM relation ID 1370699
# Confirmed boundary corners (WGS84 decimal degrees, lon/lat for shapely):
GREENWOOD_RELATION_ID = 1370699
GREENWOOD_POLYGON = [
    (-73.988389, 40.659194),
    (-73.995194, 40.659556),
    (-74.002056, 40.652944),
    (-73.989083, 40.644250),
    (-73.980500, 40.647750),
    (-73.981917, 40.655278),
]
GREENWOOD_BBOX = (40.644250, -74.002056, 40.659556, -73.980500)  # S, W, N, E

# Overpass endpoints tried in order; first success wins.
_ENDPOINTS = [
    "https://overpass.kumi.systems/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    "https://overpass-api.de/api/interpreter",
]
_RETRIES_PER_ENDPOINT = 2
_RETRY_DELAY_S = 3


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _query(query: str):
    """Run an Overpass QL query with multi-endpoint fallback and per-endpoint retries."""
    last_err = None
    for url in _ENDPOINTS:
        for attempt in range(_RETRIES_PER_ENDPOINT):
            try:
                api = overpy.Overpass(url=url)
                return api.query(query)
            except Exception as exc:
                last_err = exc
                if attempt < _RETRIES_PER_ENDPOINT - 1:
                    time.sleep(_RETRY_DELAY_S)
        print(f"  Overpass {url} failed ({last_err}), trying next…")
        time.sleep(1)
    raise RuntimeError(f"All Overpass endpoints failed. Last error: {last_err}")


def _bbox(boundary: Polygon) -> tuple[float, float, float, float]:
    """
    Return (south, west, north, east) for an Overpass bbox filter.
    shapely .bounds → (minx=west, miny=south, maxx=east, maxy=north)
    """
    west, south, east, north = boundary.bounds
    return south, west, north, east


def _cache_path(name: str) -> str:
    return os.path.join(CACHE_DIR, f"osm_{name}.geojson")


def _boundary_cache_path() -> str:
    return os.path.join(CACHE_DIR, "osm_boundary.json")


def _save_boundary(poly: Polygon) -> None:
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(_boundary_cache_path(), "w") as f:
        json.dump(list(poly.exterior.coords), f)


def _load_boundary() -> Polygon | None:
    p = _boundary_cache_path()
    if not os.path.exists(p):
        return None
    with open(p) as f:
        coords = json.load(f)
    return Polygon(coords)


def _save_gdf(gdf: gpd.GeoDataFrame, name: str) -> None:
    os.makedirs(CACHE_DIR, exist_ok=True)
    gdf.to_file(_cache_path(name), driver="GeoJSON")


def _load_gdf(name: str) -> gpd.GeoDataFrame | None:
    p = _cache_path(name)
    if not os.path.exists(p):
        return None
    return gpd.read_file(p)


def _ways_to_polygon(relation_result, relation) -> Polygon | None:
    """Convert OSM relation ways into a merged Shapely polygon."""
    way_nodes: dict = {}
    for way in relation_result.ways:
        coords = [(float(n.lon), float(n.lat)) for n in way.nodes]
        if len(coords) >= 3:
            way_nodes[way.id] = coords

    rings = []
    for member in relation.members:
        if hasattr(member, "ref") and member.ref in way_nodes:
            coords = way_nodes[member.ref]
            if coords[0] == coords[-1] and len(coords) >= 4:
                rings.append(Polygon(coords))

    if not rings:
        return None
    merged = unary_union(rings)
    if merged.geom_type == "MultiPolygon":
        return max(merged.geoms, key=lambda g: g.area)
    return merged


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fetch_cemetery_boundary(no_cache: bool = False) -> Polygon:
    """Return the Green-Wood Cemetery boundary as a Shapely Polygon (lon/lat)."""
    if not no_cache:
        cached = _load_boundary()
        if cached is not None:
            print("Cemetery boundary loaded from cache.")
            return cached

    query = f"""
    [out:json][timeout:60];
    relation({GREENWOOD_RELATION_ID});
    out geom;
    """
    try:
        result = _query(query)
        if result.relations:
            rel = result.relations[0]
            poly = _ways_to_polygon(result, rel)
            if poly and not poly.is_empty:
                print(f"Cemetery boundary fetched: {poly.area:.6f} sq-deg")
                _save_boundary(poly)
                return poly
    except Exception as exc:
        print(f"Relation fetch failed ({exc}), using confirmed polygon fallback.")

    poly = Polygon(GREENWOOD_POLYGON)
    _save_boundary(poly)
    return poly


def fetch_trees(boundary: Polygon, no_cache: bool = False) -> gpd.GeoDataFrame:
    """Return OSM trees inside the cemetery as a GeoDataFrame of Points."""
    if not no_cache:
        cached = _load_gdf("trees")
        if cached is not None:
            print(f"Trees loaded from cache: {len(cached)}")
            return cached

    s, w, n, e = _bbox(boundary)
    query = f"""
    [out:json][timeout:60];
    node["natural"="tree"]({s},{w},{n},{e});
    out body;
    """
    result = _query(query)

    records = []
    for node in result.nodes:
        pt = Point(float(node.lon), float(node.lat))
        if boundary.contains(pt):
            raw_h = node.tags.get("height", "0").rstrip("m ").strip()
            try:
                height_m = float(raw_h) if raw_h else 0.0
            except ValueError:
                height_m = 0.0
            if height_m <= 0:
                height_m = 0.0

            if "diameter_crown" in node.tags:
                canopy_r = float(node.tags["diameter_crown"]) / 2
            else:
                # Estimate from height: mature trees ≈ 0.4× height as crown radius.
                # Default height 15 m → 6 m crown radius.
                effective_h = height_m if height_m > 0 else 15.0
                canopy_r = max(3.0, effective_h * 0.4)

            records.append({
                "geometry": pt,
                "canopy_radius_m": canopy_r,
                "height_m": height_m if height_m > 0 else None,
            })

    gdf = gpd.GeoDataFrame(records, crs="EPSG:4326")
    print(f"Trees fetched: {len(gdf)}")
    _save_gdf(gdf, "trees")
    return gdf


def fetch_buildings(boundary: Polygon, no_cache: bool = False) -> gpd.GeoDataFrame:
    """Return OSM building footprints inside the cemetery as a GeoDataFrame."""
    if not no_cache:
        cached = _load_gdf("buildings")
        if cached is not None:
            print(f"Buildings loaded from cache: {len(cached)}")
            return cached

    s, w, n, e = _bbox(boundary)
    query = f"""
    [out:json][timeout:60];
    (
      way["building"]({s},{w},{n},{e});
      relation["building"]({s},{w},{n},{e});
    );
    (._;>;);
    out body;
    """
    result = _query(query)

    records = []
    for way in result.ways:
        try:
            coords = [(float(nd.lon), float(nd.lat)) for nd in way.nodes]
        except Exception:
            continue
        if len(coords) >= 3:
            poly = Polygon(coords)
            if poly.is_valid and boundary.intersects(poly):
                records.append({"geometry": poly})

    gdf = gpd.GeoDataFrame(records, crs="EPSG:4326")
    print(f"Buildings fetched: {len(gdf)}")
    _save_gdf(gdf, "buildings")
    return gdf


def fetch_paths(boundary: Polygon, no_cache: bool = False) -> gpd.GeoDataFrame:
    """Return OSM footway/path/road geometries inside the cemetery as a GeoDataFrame."""
    if not no_cache:
        cached = _load_gdf("paths")
        if cached is not None:
            print(f"Paths loaded from cache: {len(cached)}")
            return cached

    s, w, n, e = _bbox(boundary)
    query = f"""
    [out:json][timeout:60];
    (
      way["highway"]({s},{w},{n},{e});
    );
    (._;>;);
    out body;
    """
    result = _query(query)

    records = []
    for way in result.ways:
        try:
            coords = [(float(nd.lon), float(nd.lat)) for nd in way.nodes]
        except Exception:
            continue
        if len(coords) >= 2:
            line = LineString(coords)
            if boundary.intersects(line):
                records.append({"geometry": line})

    gdf = gpd.GeoDataFrame(records, crs="EPSG:4326")
    print(f"Paths fetched: {len(gdf)}")
    _save_gdf(gdf, "paths")
    return gdf
