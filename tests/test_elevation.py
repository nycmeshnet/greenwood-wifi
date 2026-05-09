"""Tests for elevation grid coordinate transforms.

The latlon_to_grid / grid_to_latlon functions must form an exact round-trip so
that LOS ray-casting samples the correct terrain cells.  These tests will catch
any future changes to the transform convention.
"""

import pytest
from data.elevation import latlon_to_grid, grid_to_latlon

# Minimal synthetic transform that matches the module's convention:
# origin is top-left (north-west), cell_lat is negative (south = increasing row)
SAMPLE_TRANSFORM = {
    "origin_lon": -74.002,
    "origin_lat": 40.660,
    "cell_lon": 0.0001,    # ~8 m per cell
    "cell_lat": -0.0001,   # negative = rows increase southward
    "nrows": 160,
    "ncols": 200,
    "resolution_m": 10.0,
}


class TestGridTransform:
    def test_origin_maps_to_row0_col0(self):
        t = SAMPLE_TRANSFORM
        r, c = latlon_to_grid(t["origin_lat"], t["origin_lon"], t)
        assert r == pytest.approx(0.0, abs=1e-9)
        assert c == pytest.approx(0.0, abs=1e-9)

    def test_round_trip_interior_point(self):
        t = SAMPLE_TRANSFORM
        lat_in, lon_in = 40.650, -73.990
        r, c = latlon_to_grid(lat_in, lon_in, t)
        lat_out, lon_out = grid_to_latlon(r, c, t)
        assert lat_out == pytest.approx(lat_in, abs=1e-9)
        assert lon_out == pytest.approx(lon_in, abs=1e-9)

    def test_southward_increases_row(self):
        t = SAMPLE_TRANSFORM
        r_north, _ = latlon_to_grid(40.660, -73.990, t)
        r_south, _ = latlon_to_grid(40.640, -73.990, t)
        assert r_south > r_north, "More southerly point should have higher row index"

    def test_eastward_increases_col(self):
        t = SAMPLE_TRANSFORM
        _, c_west = latlon_to_grid(40.650, -74.000, t)
        _, c_east = latlon_to_grid(40.650, -73.980, t)
        assert c_east > c_west, "More easterly point should have higher column index"

    def test_cell_lat_is_negative(self):
        """cell_lat must be negative so rows increase toward south."""
        assert SAMPLE_TRANSFORM["cell_lat"] < 0

    def test_known_offset(self):
        """Moving exactly one cell south increases row by exactly 1."""
        t = SAMPLE_TRANSFORM
        lat0 = t["origin_lat"]
        r0, _ = latlon_to_grid(lat0, t["origin_lon"], t)
        r1, _ = latlon_to_grid(lat0 + t["cell_lat"], t["origin_lon"], t)
        assert r1 == pytest.approx(r0 + 1.0, abs=1e-9)
