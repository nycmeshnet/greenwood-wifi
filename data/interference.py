"""Optional: fetch existing WiFi AP density from WiGLE for RF interference context."""

import numpy as np
import requests
from shapely.geometry import Polygon

WIGLE_URL = "https://api.wigle.net/api/v2/network/search"


def fetch_wigle_density(
    boundary: Polygon,
    api_key: str | None,
    grid_size: int = 20,
) -> np.ndarray | None:
    """
    Query WiGLE for AP positions inside the cemetery bbox.

    Returns a (grid_size × grid_size) float array normalised to [0, 1]
    representing relative AP density, or None if no key is provided.

    WiGLE free tier allows ~100 queries/day; this uses a single bbox query
    and bins the results, so it costs exactly 1 request.
    """
    if not api_key:
        return None

    minx, miny, maxx, maxy = boundary.bounds
    params = {
        "latrange1": miny,
        "latrange2": maxy,
        "longrange1": minx,
        "longrange2": maxx,
        "resultsPerPage": 1000,
        "freenet": "false",
        "paynet": "false",
    }
    headers = {"Authorization": f"Basic {api_key}"}

    try:
        r = requests.get(WIGLE_URL, params=params, headers=headers, timeout=30)
        r.raise_for_status()
        results = r.json().get("results", [])
    except Exception as exc:
        print(f"WiGLE fetch failed ({exc}) — skipping interference layer.")
        return None

    density = np.zeros((grid_size, grid_size), dtype=np.float32)
    lat_step = (maxy - miny) / grid_size
    lon_step = (maxx - minx) / grid_size

    for ap in results:
        row = int((float(ap["trilat"]) - miny) / lat_step)
        col = int((float(ap["trilong"]) - minx) / lon_step)
        if 0 <= row < grid_size and 0 <= col < grid_size:
            density[row, col] += 1.0

    if density.max() > 0:
        density /= density.max()

    print(f"WiGLE: {len(results)} APs found, interference grid computed.")
    return density
