"""Tests for OSM data fetching helpers.

These tests guard against coordinate-order bugs (the original code had lat/lon
swapped in the Overpass bbox) so that porting to a new site doesn't silently
query the wrong area.
"""

import pytest
from shapely.geometry import Polygon

from data.osm import _bbox, _FALLBACK_POLYGON
from constants import CEMETERY_BBOX


def _make_polygon(south, west, north, east):
    """Build a lon/lat shapely polygon from a bbox."""
    return Polygon([(west, south), (east, south), (east, north), (west, north)])


class TestBboxHelper:
    def test_south_is_latitude(self):
        p = _make_polygon(40.64, -74.00, 40.66, -73.98)
        s, w, n, e = _bbox(p)
        assert 40 < s < 41, f"south should be a latitude (~40.x), got {s}"

    def test_west_is_longitude(self):
        p = _make_polygon(40.64, -74.00, 40.66, -73.98)
        s, w, n, e = _bbox(p)
        assert -75 < w < -73, f"west should be a longitude (~-74.x), got {w}"

    def test_south_less_than_north(self):
        p = _make_polygon(40.64, -74.00, 40.66, -73.98)
        s, w, n, e = _bbox(p)
        assert s < n, f"south {s} must be less than north {n}"

    def test_west_less_than_east(self):
        p = _make_polygon(40.64, -74.00, 40.66, -73.98)
        s, w, n, e = _bbox(p)
        assert w < e, f"west {w} must be less than east {e}"

    def test_order_is_south_west_north_east(self):
        """Overpass bbox format is (south, west, north, east) — not (west, south, east, north)."""
        p = _make_polygon(10.0, 20.0, 30.0, 40.0)
        s, w, n, e = _bbox(p)
        assert s == pytest.approx(10.0)
        assert w == pytest.approx(20.0)
        assert n == pytest.approx(30.0)
        assert e == pytest.approx(40.0)

    def test_greenwood_bbox_is_in_brooklyn(self):
        p = Polygon(_FALLBACK_POLYGON)
        s, w, n, e = _bbox(p)
        assert 40.6 < s < 40.7, "Green-Wood south edge should be ~40.64°N"
        assert -74.1 < w < -73.9, "Green-Wood west edge should be ~-74.00°W"
        assert 40.6 < n < 40.7, "Green-Wood north edge should be ~40.66°N"
        assert -74.1 < e < -73.9, "Green-Wood east edge should be ~-73.98°W"

    def test_polygon_fallback_is_valid(self):
        p = Polygon(_FALLBACK_POLYGON)
        assert p.is_valid
        assert not p.is_empty
        # Area should be roughly the cemetery (~190 ha = ~1.9 km²).
        # At 40.65°N: 1 sq-deg ≈ 111 km × 84 km = 9324 km² → 1.9 km² ≈ 0.0002 sq-deg
        assert 0.0001 < p.area < 0.001, f"Unexpected polygon area: {p.area}"

    def test_bbox_constants_match_polygon(self):
        s_bbox, w_bbox, n_bbox, e_bbox = CEMETERY_BBOX
        p = Polygon(_FALLBACK_POLYGON)
        s_poly, w_poly, n_poly, e_poly = _bbox(p)
        assert abs(s_bbox - s_poly) < 0.001
        assert abs(w_bbox - w_poly) < 0.001
        assert abs(n_bbox - n_poly) < 0.001
        assert abs(e_bbox - e_poly) < 0.001
