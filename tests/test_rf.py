"""Tests for the RF propagation model.

These tests ensure that effective_range_m behaves physically correctly so that
porting to new frequency bands or range values doesn't silently produce nonsense.
"""

import pytest
from models.rf import effective_range_m, effective_range_ft, FT_PER_M


class TestEffectiveRange:
    def test_no_vegetation_returns_rated_range(self):
        assert effective_range_m(150.0, 2.4, 0.0) == pytest.approx(150.0)
        assert effective_range_m(80.0, 5.0, 0.0) == pytest.approx(80.0)
        assert effective_range_m(60.0, 6.0, 0.0) == pytest.approx(60.0)

    def test_vegetation_reduces_range(self):
        for freq in (2.4, 5.0, 6.0):
            r_clear = effective_range_m(100.0, freq, 0.0)
            r_veg = effective_range_m(100.0, freq, 50.0)
            assert r_veg < r_clear, f"Range should decrease with vegetation at {freq} GHz"

    def test_higher_frequency_more_attenuation(self):
        """6 GHz loses more range per metre of vegetation than 2.4 GHz."""
        veg = 30.0
        r_24 = effective_range_m(100.0, 2.4, veg)
        r_5 = effective_range_m(100.0, 5.0, veg)
        r_6 = effective_range_m(100.0, 6.0, veg)
        assert r_24 > r_5 > r_6, "Higher frequency should suffer more vegetation loss"

    def test_range_never_negative(self):
        # Even with extreme vegetation, range must stay non-negative
        r = effective_range_m(100.0, 6.0, 10_000.0)
        assert r >= 0.0

    def test_range_never_exceeds_rated(self):
        r = effective_range_m(100.0, 2.4, 0.0)
        assert r <= 100.0 + 1e-9

    def test_ft_wrapper_unit_conversion(self):
        rated_ft = 500.0
        rated_m = rated_ft / FT_PER_M
        result_ft = effective_range_ft(rated_ft, 2.4, 0.0)
        result_m = effective_range_m(rated_m, 2.4, 0.0)
        assert result_ft == pytest.approx(result_m * FT_PER_M, rel=1e-6)

    def test_ft_to_m_constant(self):
        assert FT_PER_M == pytest.approx(3.28084, abs=0.0001)

    def test_500ft_equals_152m(self):
        assert 500.0 / FT_PER_M == pytest.approx(152.4, abs=0.1)

    def test_vectorised_numpy_input(self):
        import numpy as np
        veg = np.array([0.0, 10.0, 50.0, 100.0])
        result = effective_range_m(100.0, 2.4, veg)
        assert result.shape == (4,)
        assert (result[:-1] >= result[1:]).all(), "Range should decrease monotonically"
