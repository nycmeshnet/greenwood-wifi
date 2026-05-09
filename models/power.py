"""Solar power budget model for a single AP node."""

import numpy as np

from constants import (
    PANEL_RATED_W, LOAD_W, BATTERY_WH, DERATING,
    DEMAND_WH_PER_DAY, MARGINAL_LOW, MARGINAL_HIGH,
)

# Re-export so callers that import from here still work.
__all__ = [
    "PANEL_RATED_W", "LOAD_W", "BATTERY_WH", "DERATING",
    "DEMAND_WH_PER_DAY", "daily_harvest_wh", "solar_status",
    "best_panel_azimuth", "facing_label",
]


def daily_harvest_wh(ghi_wh_per_day: float, shade_fraction: float) -> float:
    """
    Estimate daily solar energy yield in Wh.

    Uses a standard peak-sun-hour model:
        harvest = (GHI / 1000 W/m²) × panel_rated_W × (1 - shade) × derating

    ghi_wh_per_day  Global Horizontal Irradiance in Wh/m²/day
                    (e.g. 3 peak-sun-hours = 3000 Wh/m²/day)
    shade_fraction  Fraction of upward hemisphere blocked by tree canopy [0–1]
    """
    return (ghi_wh_per_day / 1000.0) * PANEL_RATED_W * (1.0 - shade_fraction) * DERATING


_FACING_LABELS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]


def facing_label(azimuth_deg: float) -> str:
    """Convert a compass azimuth (0–360°) into an 8-rose direction label."""
    az = azimuth_deg % 360.0
    return _FACING_LABELS[int((az + 22.5) // 45) % 8]


def best_panel_azimuth(
    directional_shade: np.ndarray,
    latitude_deg: float = 40.65,
    default_azimuth: float = 180.0,
) -> tuple[float, float]:
    """
    Pick the panel azimuth that maximises sun exposure given local tree shade.

    Parameters
    ----------
    directional_shade : array of length N — shade fraction (0–1) per azimuth
                        bucket (bucket i is centred on i × 360°/N).
    latitude_deg      : site latitude. Northern hemisphere → sun comes from the
                        south; southern hemisphere → from the north.
    default_azimuth   : returned when there are no trees and no signal.

    Returns
    -------
    (azimuth_deg, score) where score is the relative yield (1.0 = no shade).
    """
    n_buckets = len(directional_shade)
    if n_buckets == 0:
        return default_azimuth, 1.0

    bucket_centers = np.arange(n_buckets) * (360.0 / n_buckets)
    sun_center = 180.0 if latitude_deg >= 0 else 0.0

    # Sun-time prior: cos² lobe centred on the equatorial direction. Captures
    # the fact that the sun spends most daylight hours near solar noon and
    # rapidly less time at extreme east/west azimuths.
    sun_delta = np.minimum(
        np.abs(bucket_centers - sun_center),
        360.0 - np.abs(bucket_centers - sun_center),
    )
    sun_weight = np.maximum(0.0, np.cos(np.radians(sun_delta))) ** 2

    candidates = np.arange(0.0, 360.0, 360.0 / n_buckets)
    best_az, best_score = default_azimuth, -np.inf
    sun_norm = sun_weight.sum() or 1.0
    for phi in candidates:
        delta = np.minimum(
            np.abs(bucket_centers - phi),
            360.0 - np.abs(bucket_centers - phi),
        )
        accept = np.maximum(0.0, np.cos(np.radians(delta)))
        # Score = expected yield = ∫ acceptance × sun-availability × sky-clearness
        score = float(((1.0 - directional_shade) * accept * sun_weight).sum() / sun_norm)
        if score > best_score:
            best_score = score
            best_az = float(phi)
    return best_az, best_score


def solar_status(harvest_wh: float) -> str:
    """
    Classify a candidate location by its solar viability.

    Returns one of:
        'viable'   — harvest ≥ 110 % of daily demand; healthy margin
        'marginal' — harvest 80–110 % of demand; prefer alternative if one exists
        'unviable' — harvest < 80 % of demand; exclude from candidates
    """
    ratio = harvest_wh / DEMAND_WH_PER_DAY
    if ratio >= MARGINAL_HIGH:
        return "viable"
    if ratio >= MARGINAL_LOW:
        return "marginal"
    return "unviable"
