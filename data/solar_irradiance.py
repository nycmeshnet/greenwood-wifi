"""Fetch monthly solar irradiance and temperature data.

Primary source: NASA POWER API (no API key, designed for solar energy apps).
Fallback: hardcoded multi-year averages for Brooklyn, NY derived from NREL/NSRDB.
Temperature in °F; GHI in Wh/m²/day (SI physics unit, not user-facing).
"""

import json
import os

import requests

CACHE_DIR = os.path.join(os.path.dirname(__file__), "cache")
SOLAR_CACHE = os.path.join(CACHE_DIR, "solar.json")

NASA_URL = "https://power.larc.nasa.gov/api/temporal/monthly/point"

# Brooklyn, NY — centre of Green-Wood Cemetery
DEFAULT_LAT = 40.651
DEFAULT_LON = -73.994

MONTH_NAMES = {
    1: "January", 2: "February", 3: "March", 4: "April",
    5: "May", 6: "June", 7: "July", 8: "August",
    9: "September", 10: "October", 11: "November", 12: "December",
}

# Multi-year averages for Brooklyn, NY (40.65 N) derived from NREL/NSRDB.
# GHI in Wh/m²/day; avg_temp_f in °F.
_BROOKLYN_FALLBACK = {
    1:  {"ghi_wh_per_day": 1930,  "avg_temp_f": 33.6},
    2:  {"ghi_wh_per_day": 2660,  "avg_temp_f": 35.6},
    3:  {"ghi_wh_per_day": 3780,  "avg_temp_f": 43.7},
    4:  {"ghi_wh_per_day": 4960,  "avg_temp_f": 54.0},
    5:  {"ghi_wh_per_day": 5640,  "avg_temp_f": 63.5},
    6:  {"ghi_wh_per_day": 6050,  "avg_temp_f": 72.5},
    7:  {"ghi_wh_per_day": 5930,  "avg_temp_f": 78.1},
    8:  {"ghi_wh_per_day": 5410,  "avg_temp_f": 76.8},
    9:  {"ghi_wh_per_day": 4280,  "avg_temp_f": 68.9},
    10: {"ghi_wh_per_day": 3000,  "avg_temp_f": 57.9},
    11: {"ghi_wh_per_day": 1960,  "avg_temp_f": 48.0},
    12: {"ghi_wh_per_day": 1560,  "avg_temp_f": 39.4},
}


def fetch_solar_and_temperature(
    lat: float = DEFAULT_LAT,
    lon: float = DEFAULT_LON,
    no_cache: bool = False,
) -> dict:
    """
    Return monthly solar and temperature data as:
        {month_int_str: {"ghi_wh_per_day": float, "avg_temp_f": float, "month_name": str}}

    Tries NASA POWER for live multi-year averages; falls back to hardcoded
    Brooklyn values if the request fails.
    """
    os.makedirs(CACHE_DIR, exist_ok=True)

    if not no_cache and os.path.exists(SOLAR_CACHE):
        print("Loading solar data from cache...")
        with open(SOLAR_CACHE) as f:
            return json.load(f)

    result = _fetch_nasa_power(lat, lon)
    if result is None:
        print("NASA POWER unavailable — using hardcoded Brooklyn averages.")
        result = {
            str(m): {**d, "month_name": MONTH_NAMES[m]}
            for m, d in _BROOKLYN_FALLBACK.items()
        }

    with open(SOLAR_CACHE, "w") as f:
        json.dump(result, f, indent=2)
    return result


def _fetch_nasa_power(lat: float, lon: float) -> dict | None:
    """Fetch 4-year monthly averages from NASA POWER. Returns None on any failure."""
    params = {
        "parameters": "ALLSKY_SFC_SW_DWN,T2M",
        "community": "RE",
        "longitude": lon,
        "latitude": lat,
        "format": "JSON",
        "start": "2020",
        "end": "2023",
    }
    try:
        print("Fetching solar data from NASA POWER...")
        r = requests.get(NASA_URL, params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
        ghi_raw = data["properties"]["parameter"]["ALLSKY_SFC_SW_DWN"]
        t2m_raw = data["properties"]["parameter"]["T2M"]
    except Exception as exc:
        print(f"NASA POWER fetch failed ({exc}).")
        return None

    # Keys are YYYYMM strings (e.g. "202001"). Aggregate to monthly averages.
    monthly_ghi: dict[int, list] = {m: [] for m in range(1, 13)}
    monthly_temp: dict[int, list] = {m: [] for m in range(1, 13)}

    for key, val in ghi_raw.items():
        month = int(key[4:])
        if month > 12:          # month 13 = annual average, skip
            continue
        if val is not None and val > 0:
            monthly_ghi[month].append(val * 1000.0)   # kWh/m²/day → Wh/m²/day

    for key, val in t2m_raw.items():
        month = int(key[4:])
        if month > 12:
            continue
        if val is not None:
            monthly_temp[month].append(val)

    result = {}
    for m in range(1, 13):
        ghi = sum(monthly_ghi[m]) / len(monthly_ghi[m]) if monthly_ghi[m] else 0.0
        temp_c = sum(monthly_temp[m]) / len(monthly_temp[m]) if monthly_temp[m] else 10.0
        result[str(m)] = {
            "ghi_wh_per_day": round(ghi, 1),
            "avg_temp_f": round(temp_c * 9 / 5 + 32, 1),
            "month_name": MONTH_NAMES[m],
        }

    print("Solar data fetched from NASA POWER.")
    return result


def get_design_month(
    solar_data: dict,
    freeze_threshold_f: float = 34.0,
    panel_w: float = 100.0,
    load_w: float = 10.0,
    derating: float = 0.80,
) -> tuple[int, float]:
    """
    Return (month_int, ghi_wh_per_day) for the worst-solar month where the
    unshaded panel can meet the 24/7 demand.

    Months below the freeze threshold are excluded (battery thermal cutoff).
    Months where even a perfectly unshaded panel cannot meet 24/7 demand are
    also excluded — those months will have limited operation regardless of
    placement; they are noted separately in the summary.

    freeze_threshold_f: 34 °F rather than 32 °F because nightly freezes are
    common when the monthly average is near 32 °F.
    """
    demand_wh = load_w * 24.0
    # Minimum GHI so a fully-exposed panel meets demand
    min_viable_ghi = demand_wh / ((panel_w / 1000.0) * derating)   # Wh/m²/day

    candidates = {
        int(m): d
        for m, d in solar_data.items()
        if d["avg_temp_f"] > freeze_threshold_f
        and d["ghi_wh_per_day"] >= min_viable_ghi
    }

    if not candidates:
        # Fall back to warmest above-freeze month and warn
        above_freeze = {int(m): d for m, d in solar_data.items()
                        if d["avg_temp_f"] > freeze_threshold_f}
        if not above_freeze:
            raise ValueError("No above-freezing months found in solar data.")
        worst = max(above_freeze, key=lambda m: above_freeze[m]["ghi_wh_per_day"])
        print(
            "WARNING: No above-freezing month has enough sun for 24/7 operation with "
            f"this hardware. Using {solar_data[str(worst)]['month_name']} as design "
            "month. System will not run 24/7 at any location."
        )
        return worst, above_freeze[worst]["ghi_wh_per_day"]

    worst = min(candidates, key=lambda m: candidates[m]["ghi_wh_per_day"])
    return worst, candidates[worst]["ghi_wh_per_day"]


def shoulder_months(
    solar_data: dict,
    freeze_threshold_f: float = 34.0,
    panel_w: float = 100.0,
    load_w: float = 10.0,
    derating: float = 0.80,
) -> list[str]:
    """
    Return names of above-freezing months where the unshaded panel cannot meet
    24/7 demand — these are 'shoulder months' with limited operation.
    """
    demand_wh = load_w * 24.0
    min_viable_ghi = demand_wh / ((panel_w / 1000.0) * derating)

    return [
        d["month_name"]
        for m, d in solar_data.items()
        if d["avg_temp_f"] > freeze_threshold_f
        and d["ghi_wh_per_day"] < min_viable_ghi
    ]
