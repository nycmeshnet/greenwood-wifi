"""Tests for the solar power budget model.

Ensures the harvest formula and status thresholds are correct so that
swapping hardware specs (panel size, load, battery) doesn't silently
produce wrong viability classifications.
"""

import pytest
from models.power import daily_harvest_wh, solar_status, DEMAND_WH_PER_DAY, PANEL_RATED_W, LOAD_W, DERATING


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
