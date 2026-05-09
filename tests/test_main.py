"""Tests for main.py helpers.

No network calls — uses a small synthetic polygon.
Guards against points escaping the boundary and feet/metres unit confusion.
"""

import pytest
from shapely.geometry import Point, Polygon

from main import generate_grid, FT_PER_M


def _small_brooklyn_polygon():
    """~500 m × 450 m rectangle in Green-Wood to keep test grid small."""
    return Polygon([
        (-73.995, 40.648),
        (-73.990, 40.648),
        (-73.990, 40.652),
        (-73.995, 40.652),
    ])


class TestGenerateGrid:
    def test_all_points_inside_boundary(self):
        boundary = _small_brooklyn_polygon()
        points = generate_grid(boundary, spacing_m=50)
        for lat, lon in points:
            assert boundary.contains(Point(lon, lat)), (
                f"({lat:.6f}, {lon:.6f}) is outside the boundary polygon"
            )

    def test_returns_lat_lon_order(self):
        """First element of each tuple must be latitude (~40.x), second longitude (~-74.x)."""
        boundary = _small_brooklyn_polygon()
        points = generate_grid(boundary, spacing_m=50)
        assert len(points) > 0, "Grid should contain at least one point"
        for lat, lon in points:
            assert 40.0 < lat < 41.0, f"Expected latitude ~40.x, got {lat}"
            assert -74.1 < lon < -73.9, f"Expected longitude ~-74.x, got {lon}"

    def test_coarser_spacing_fewer_points(self):
        boundary = _small_brooklyn_polygon()
        fine = generate_grid(boundary, spacing_m=30)
        coarse = generate_grid(boundary, spacing_m=150)
        assert len(fine) > len(coarse)

    def test_non_rectangular_polygon(self):
        """Triangular polygon — no point should fall outside."""
        triangle = Polygon([(-73.995, 40.648), (-73.990, 40.648), (-73.993, 40.652)])
        points = generate_grid(triangle, spacing_m=50)
        for lat, lon in points:
            assert triangle.contains(Point(lon, lat))


class TestFeetMetresConversion:
    def test_500ft_is_152m(self):
        assert 500.0 / FT_PER_M == pytest.approx(152.4, abs=0.1)

    def test_260ft_is_79m(self):
        assert 260.0 / FT_PER_M == pytest.approx(79.2, abs=0.1)

    def test_ft_per_m_value(self):
        assert FT_PER_M == pytest.approx(3.28084, abs=0.00001)

    def test_round_trip(self):
        for ft in (100.0, 250.0, 500.0, 1000.0):
            assert (ft / FT_PER_M) * FT_PER_M == pytest.approx(ft, rel=1e-9)

    def test_metres_always_less_than_feet(self):
        """1 ft < 1 m, so range_m must always be less than range_ft."""
        for ft in (100.0, 300.0, 500.0):
            assert ft / FT_PER_M < ft
