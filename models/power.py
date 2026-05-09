"""Solar power budget model for a single AP node."""

# Hardware constants (fixed per the project spec)
PANEL_RATED_W = 100.0
LOAD_W = 10.0
BATTERY_WH = 50.0
DERATING = 0.80          # accounts for temperature, wiring, soiling losses
DEMAND_WH_PER_DAY = LOAD_W * 24.0   # 240 Wh/day

# Thresholds for classifying a candidate location
_MARGINAL_LOW = 0.80     # below this ratio → unviable
_MARGINAL_HIGH = 1.10    # above this ratio → clearly viable


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


def solar_status(harvest_wh: float) -> str:
    """
    Classify a candidate location by its solar viability.

    Returns one of:
        'viable'   — harvest ≥ 110 % of daily demand; healthy margin
        'marginal' — harvest 80–110 % of demand; prefer alternative if one exists
        'unviable' — harvest < 80 % of demand; exclude from candidates
    """
    ratio = harvest_wh / DEMAND_WH_PER_DAY
    if ratio >= _MARGINAL_HIGH:
        return "viable"
    if ratio >= _MARGINAL_LOW:
        return "marginal"
    return "unviable"
