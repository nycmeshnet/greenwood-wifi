"""Tests for solar irradiance helpers.

Guards the design-month selection logic: the most common failure mode when
porting to a new location is picking a month that is above freezing but has
insufficient sun, causing the optimizer to find zero viable candidates.
"""

import pytest
from data.solar_irradiance import get_design_month, shoulder_months, _BROOKLYN_FALLBACK


def _make_solar_data(months: dict) -> dict:
    """Build a minimal solar_data dict from {month_int: (ghi, temp_f)} tuples."""
    return {
        str(m): {"ghi_wh_per_day": ghi, "avg_temp_f": temp, "month_name": f"Month{m}"}
        for m, (ghi, temp) in months.items()
    }


class TestDesignMonth:
    def test_picks_worst_viable_month(self):
        # March=3000 Wh (breakeven), April=4000 Wh — both above freeze.
        # Design month should be March (lower GHI).
        data = _make_solar_data({3: (3000, 43.0), 4: (4000, 53.0)})
        month, ghi = get_design_month(data)
        assert month == 3
        assert ghi == pytest.approx(3000.0)

    def test_excludes_below_freeze_months(self):
        # January is above-freeze but frozen (temp=30°F < 34°F threshold)
        data = _make_solar_data({1: (3500, 30.0), 5: (5000, 63.0)})
        month, ghi = get_design_month(data)
        assert month == 5, "January (30°F) should be excluded"

    def test_excludes_months_with_insufficient_sun(self):
        # November has enough temp (48°F) but not enough GHI (1960 Wh < 3000 threshold)
        # October has exactly 3000 Wh — should be chosen
        data = _make_solar_data({10: (3000, 57.0), 11: (1960, 48.0)})
        month, ghi = get_design_month(data)
        assert month == 10, "November should be excluded due to insufficient GHI"

    def test_fallback_when_no_viable_month(self):
        # All months have insufficient sun — should return best available and not crash
        data = _make_solar_data({4: (1000, 53.0), 5: (1500, 63.0)})
        month, ghi = get_design_month(data)   # should not raise
        assert month in (4, 5)

    def test_design_month_ghi_meets_demand_threshold(self):
        # 100W panel, 10W load, 0.80 derating → min GHI = 3000 Wh/m²/day
        data = _make_solar_data({9: (4000, 68.0), 10: (3100, 57.0)})
        month, ghi = get_design_month(data)
        # The returned GHI must be high enough that an unshaded panel meets demand
        min_viable = (10 * 24) / ((100 / 1000) * 0.80)
        assert ghi >= min_viable, f"Design month GHI {ghi} < min viable {min_viable}"

    def test_brooklyn_fallback_has_valid_design_month(self):
        data = {
            str(m): {**d, "month_name": f"Month{m}"}
            for m, d in _BROOKLYN_FALLBACK.items()
        }
        month, ghi = get_design_month(data)
        assert 1 <= month <= 12
        assert ghi > 0


class TestShoulderMonths:
    def test_november_december_are_shoulder_months_in_brooklyn(self):
        from data.solar_irradiance import MONTH_NAMES
        data = {
            str(m): {**d, "month_name": MONTH_NAMES[m]}
            for m, d in _BROOKLYN_FALLBACK.items()
        }
        shoulders = shoulder_months(data)
        # Nov (1960 Wh) and Dec (1560 Wh) are above-freeze but below 3000 Wh threshold
        assert "November" in shoulders
        assert "December" in shoulders

    def test_summer_months_are_not_shoulder_months(self):
        data = {
            str(m): {**d, "month_name": f"Month{m}"}
            for m, d in _BROOKLYN_FALLBACK.items()
        }
        shoulders = shoulder_months(data)
        for month_name in ("May", "June", "July", "August", "September"):
            assert month_name not in shoulders, f"{month_name} should not be a shoulder month"

    def test_below_freeze_months_not_in_shoulder(self):
        # Months below freeze threshold are excluded entirely, not labeled as shoulder
        data = _make_solar_data({1: (500, 30.0), 6: (5000, 72.0)})
        shoulders = shoulder_months(data)
        assert "Month1" not in shoulders
