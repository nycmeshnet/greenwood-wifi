"""Fetch bare-earth elevation raster from USGS 3DEP for Green-Wood Cemetery."""

import io
import json
import os

import numpy as np
import requests
import rasterio
from rasterio.io import MemoryFile
from shapely.geometry import Polygon

CACHE_DIR = os.path.join(os.path.dirname(__file__), "cache")
ELEV_CACHE = os.path.join(CACHE_DIR, "elevation.npy")
TRANSFORM_CACHE = os.path.join(CACHE_DIR, "elevation_transform.json")

USGS_URL = (
    "https://elevation.nationalmap.gov/arcgis/rest/services"
    "/3DEPElevation/ImageServer/exportImage"
)
RESOLUTION_M = 5.0
BBOX_PAD_DEG = 0.002


def fetch_elevation_raster(
    boundary: Polygon, no_cache: bool = False
) -> tuple[np.ndarray, dict]:
    """
    Download a bare-earth elevation raster from USGS 3DEP.

    Returns (elevation_m, transform) where transform describes the grid origin
    and cell size in degrees so any lat/lon can be mapped to a grid index.
    """
    os.makedirs(CACHE_DIR, exist_ok=True)

    if not no_cache and os.path.exists(ELEV_CACHE) and os.path.exists(TRANSFORM_CACHE):
        print("Loading elevation from cache...")
        elev = np.load(ELEV_CACHE)
        with open(TRANSFORM_CACHE) as f:
            transform = json.load(f)
        return elev, transform

    minx, miny, maxx, maxy = boundary.bounds
    minx -= BBOX_PAD_DEG
    miny -= BBOX_PAD_DEG
    maxx += BBOX_PAD_DEG
    maxy += BBOX_PAD_DEG

    mid_lat = (miny + maxy) / 2
    lat_m_per_deg = 111_320.0
    lon_m_per_deg = 111_320.0 * np.cos(np.radians(mid_lat))

    nrows = max(50, int((maxy - miny) * lat_m_per_deg / RESOLUTION_M))
    ncols = max(50, int((maxx - minx) * lon_m_per_deg / RESOLUTION_M))

    params = {
        "bbox": f"{minx},{miny},{maxx},{maxy}",
        "bboxSR": "4326",
        "size": f"{ncols},{nrows}",
        "imageSR": "4326",
        "format": "tiff",
        "pixelType": "F32",
        "noData": "-9999",
        "f": "image",
    }

    print(f"Downloading elevation raster ({ncols}×{nrows} px at {RESOLUTION_M:.0f} m)...")
    resp = requests.get(USGS_URL, params=params, timeout=120)
    resp.raise_for_status()

    with MemoryFile(resp.content) as memfile:
        with memfile.open() as ds:
            elev = ds.read(1).astype(np.float32)

    elev[elev < -100] = np.nan

    transform = {
        "origin_lon": minx,
        "origin_lat": maxy,
        "cell_lon": (maxx - minx) / ncols,
        "cell_lat": -((maxy - miny) / nrows),
        "nrows": nrows,
        "ncols": ncols,
        "resolution_m": RESOLUTION_M,
    }

    np.save(ELEV_CACHE, elev)
    with open(TRANSFORM_CACHE, "w") as f:
        json.dump(transform, f, indent=2)

    print(f"Elevation cached: min={np.nanmin(elev):.1f} m, max={np.nanmax(elev):.1f} m")
    return elev, transform


def latlon_to_grid(lat, lon, transform: dict):
    """Convert lat/lon (scalar or array) to fractional (row, col) grid indices."""
    row = (lat - transform["origin_lat"]) / transform["cell_lat"]
    col = (lon - transform["origin_lon"]) / transform["cell_lon"]
    return row, col


def grid_to_latlon(row, col, transform: dict):
    """Convert (row, col) grid indices to (lat, lon)."""
    lat = transform["origin_lat"] + row * transform["cell_lat"]
    lon = transform["origin_lon"] + col * transform["cell_lon"]
    return lat, lon
