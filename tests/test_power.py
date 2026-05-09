"""Tests for the solar power budget model.

Ensures the harvest formula and status thresholds are correct so that
swapping hardware specs (panel size, load, battery) doesn't silently
produce wrong viability classifications.
"""

import numpy as np
import pytest
from models.power import (
    daily_harvest_wh, solar_status, DEMAND_WH_PER_DAY,
    PANEL_RATED_W, LOAD_W, DERATING,
    best_panel_azimuth, facing_label,
)


class TestHarvest:
    def test_no_shade_october_brooklyn(self):
        # October GHI ~3000 Wh/m²/day; should yield exactly 240 Wh (breakeven)
        ghi = 3000.0
        harvest = daily_harvest_wh(ghi, shade_fraction=0.0)
        expected = (ghi / 1000.0) * PANEL_RATED_W * DERATING
        assert harvest == pytest.approx(expected, rel=1e-6)

    def test_full_shade_yields_zero(self):
        assert daily_harvest_wh(5000.0, shade_fraction=1.0) == pytest.approx(0.0)

    def test_shade_reduces_harvest_proportionally(self):
        base = daily_harvest_wh(4000.0, 0.0)
        half = daily_harvest_wh(4000.0, 0.5)
        assert half == pytest.approx(base * 0.5, rel=1e-6)

    def test_demand_constant(self):
        """Daily demand must equal LOAD_W × 24 h."""
        assert DEMAND_WH_PER_DAY == pytest.approx(LOAD_W * 24.0)

    def test_breakeven_ghi(self):
        """harvest == demand when GHI = demand / (panel/1000 * derating)."""
        breakeven_ghi = DEMAND_WH_PER_DAY / ((PANEL_RATED_W / 1000.0) * DERATING)
        harvest = daily_harvest_wh(breakeven_ghi, 0.0)
        assert harvest == pytest.approx(DEMAND_WH_PER_DAY, rel=1e-6)


class TestSolarStatus:
    def test_viable_above_110pct(self):
        harvest = DEMAND_WH_PER_DAY * 1.15
        assert solar_status(harvest) == "viable"

    def test_marginal_between_80_and_110pct(self):
        for ratio in (0.80, 0.95, 1.09):
            harvest = DEMAND_WH_PER_DAY * ratio
            assert solar_status(harvest) == "marginal", f"ratio={ratio} should be marginal"

    def test_unviable_below_80pct(self):
        harvest = DEMAND_WH_PER_DAY * 0.79
        assert solar_status(harvest) == "unviable"

    def test_boundary_at_80pct_is_marginal(self):
        harvest = DEMAND_WH_PER_DAY * 0.80
        assert solar_status(harvest) == "marginal"

    def test_boundary_at_110pct_is_viable(self):
        harvest = DEMAND_WH_PER_DAY * 1.10
        assert solar_status(harvest) == "viable"

    def test_zero_harvest_is_unviable(self):
        assert solar_status(0.0) == "unviable"


class TestFacingLabel:
    def test_cardinal_directions(self):
        assert facing_label(0.0)   == "N"
        assert facing_label(90.0)  == "E"
        assert facing_label(180.0) == "S"
        assert facing_label(270.0) == "W"

    def test_intercardinals(self):
        assert facing_label(45.0)  == "NE"
        assert facing_label(135.0) == "SE"
        assert facing_label(225.0) == "SW"
        assert facing_label(315.0) == "NW"

    def test_wraps_around(self):
        assert facing_label(360.0) == "N"
        assert facing_label(-1.0)  == "N"


class TestBestPanelAzimuth:
    N_BUCKETS = 24

    def test_no_shade_picks_south_in_nh(self):
        shade = np.zeros(self.N_BUCKETS)
        az, score = best_panel_azimuth(shade, latitude_deg=40.65)
        assert az == pytest.approx(180.0)
        # Theoretical max = ∫cos³ / ∫cos² = 8/(3π) ≈ 0.849 (cosine acceptance
        # weighted against cos² sun prior). The clear-sky score must reach it.
        assert score == pytest.approx(8.0 / (3.0 * np.pi), abs=0.02)

    def test_no_shade_picks_north_in_sh(self):
        shade = np.zeros(self.N_BUCKETS)
        az, _ = best_panel_azimuth(shade, latitude_deg=-33.0)
        assert az == pytest.approx(0.0)

    def test_asymmetric_block_swings_optimum_to_clear_side(self):
        # Block the south + the east half (buckets 0–13 inclusive ≈ 0°–195°).
        # The only clear sky lies west-of-south, so the optimum azimuth must
        # swing west of due-south.
        shade = np.zeros(self.N_BUCKETS)
        shade[0:14] = 1.0
        az, _ = best_panel_azimuth(shade, latitude_deg=40.65)
        assert 200.0 <= az <= 280.0, f"Expected azimuth in west-of-south, got {az}"

    def test_score_falls_when_shade_increases(self):
        clear = np.zeros(self.N_BUCKETS)
        partial = np.full(self.N_BUCKETS, 0.5)
        _, s_clear = best_panel_azimuth(clear)
        _, s_partial = best_panel_azimuth(partial)
        assert s_partial < s_clear

    def test_empty_buckets_returns_default(self):
        az, score = best_panel_azimuth(np.array([]))
        assert az == pytest.approx(180.0)
        assert score == pytest.approx(1.0)
