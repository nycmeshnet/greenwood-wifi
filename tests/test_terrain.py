"""Tests for TerrainModel construction and LOS calculations.

Uses a small synthetic elevation array so no external data is needed.
Guards against shape errors and div-by-zero when a new site has sparse
or missing OSM tree/building data.
"""

import numpy as np
import pytest
import geopandas as gpd
from shapely.geometry import Point

from models.terrain import TerrainModel

# Synthetic 20×20 flat terrain at 10 m elevation.
_NROWS, _NCOLS = 20, 20
_TRANSFORM = {
    "origin_lon": -74.002,
    "origin_lat": 40.660,
    "cell_lon":  0.001,
    "cell_lat": -0.001,
    "nrows": _NROWS,
    "ncols": _NCOLS,
    "resolution_m": 100.0,
}
_FLAT_ELEV = np.full((_NROWS, _NCOLS), 10.0, dtype=np.float32)


def _empty_gdf(columns):
    return gpd.GeoDataFrame(columns=columns, crs="EPSG:4326")


def _make_model(trees_gdf=None, buildings_gdf=None):
    if trees_gdf is None:
        trees_gdf = _empty_gdf(["geometry", "canopy_radius_m", "height_m"])
    if buildings_gdf is None:
        buildings_gdf = _empty_gdf(["geometry"])
    return TerrainModel(_FLAT_ELEV.copy(), _TRANSFORM, trees_gdf, buildings_gdf)


class TestShadeFunction:
    def test_no_trees_returns_zero(self):
        model = _make_model()
        shade = model.shade_fraction(40.655, -73.995)
        assert shade == pytest.approx(0.0)

    def test_shade_between_zero_and_one(self):
        # Even with a tree directly overhead the result must stay in [0, 1]
        trees = gpd.GeoDataFrame(
            [{"geometry": Point(-73.995, 40.655), "canopy_radius_m": 5.0, "height_m": 15.0}],
            crs="EPSG:4326",
        )
        model = _make_model(trees_gdf=trees)
        shade = model.shade_fraction(40.655, -73.995)
        assert 0.0 <= shade <= 1.0

    def test_no_buildings_returns_valid_mask(self):
        model = _make_model()
        assert model.building_mask.shape == (_NROWS, _NCOLS)
        assert model.building_mask.dtype == bool
        assert not model.building_mask.any(), "No buildings should mean empty mask"


class TestBatchLOS:
    def test_output_shape_matches_input(self):
        model = _make_model()
        ap_lat, ap_lon = 40.655, -73.995
        test_lats = np.array([40.650, 40.652, 40.654])
        test_lons = np.array([-73.992, -73.993, -73.994])
        los_clear, veg_path = model.batch_los(ap_lat, ap_lon, test_lats, test_lons)
        assert los_clear.shape == (3,)
        assert veg_path.shape == (3,)

    def test_output_types(self):
        model = _make_model()
        test_lats = np.array([40.652])
        test_lons = np.array([-73.993])
        los_clear, veg_path = model.batch_los(40.655, -73.995, test_lats, test_lons)
        assert los_clear.dtype == bool
        assert veg_path.dtype == float or np.issubdtype(veg_path.dtype, np.floating)

    def test_veg_path_non_negative(self):
        model = _make_model()
        test_lats = np.array([40.650, 40.652, 40.654])
        test_lons = np.array([-73.992, -73.993, -73.994])
        _, veg_path = model.batch_los(40.655, -73.995, test_lats, test_lons)
        assert (veg_path >= 0).all(), "Vegetation path length must be non-negative"

    def test_flat_terrain_no_trees_all_clear(self):
        model = _make_model()
        test_lats = np.array([40.651, 40.653])
        test_lons = np.array([-73.994, -73.996])
        los_clear, _ = model.batch_los(40.656, -73.998, test_lats, test_lons)
        assert los_clear.all(), "Flat terrain with no obstacles should always be LOS clear"
