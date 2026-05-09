"""Digital Surface Model construction and vectorised line-of-sight calculations."""

import warnings
import numpy as np
import scipy.ndimage
import geopandas as gpd
from pyproj import Transformer
from shapely.geometry import Point

from data.elevation import latlon_to_grid, grid_to_latlon

AP_HEIGHT_M = 0.9144      # 3 ft — AP antenna mounted on a pole
RX_HEIGHT_M = 1.524       # 5 ft — person holding a device
DEFAULT_TREE_HEIGHT_M = 15.0

_to_utm = Transformer.from_crs("EPSG:4326", "EPSG:32618", always_xy=True)
_to_wgs84 = Transformer.from_crs("EPSG:32618", "EPSG:4326", always_xy=True)


def to_utm(lon, lat):
    """Convert (lon, lat) → (easting, northing) in UTM zone 18N (metres)."""
    return _to_utm.transform(lon, lat)


def to_wgs84(easting, northing):
    """Convert (easting, northing) UTM 18N → (lon, lat)."""
    return _to_wgs84.transform(easting, northing)


class TerrainModel:
    def __init__(
        self,
        elevation: np.ndarray,
        transform: dict,
        trees_gdf: gpd.GeoDataFrame,
        buildings_gdf: gpd.GeoDataFrame,
    ):
        self.elev = elevation.astype(np.float32)
        self.transform = transform
        self.trees_gdf = trees_gdf if trees_gdf is not None else gpd.GeoDataFrame()
        self.dsm = self._build_dsm(trees_gdf)
        self.building_mask = self._rasterise_buildings(buildings_gdf)

    # ------------------------------------------------------------------
    # Construction helpers
    # ------------------------------------------------------------------

    def _build_dsm(self, trees_gdf: gpd.GeoDataFrame) -> np.ndarray:
        """Paint tree-canopy heights onto the bare-earth raster to form a DSM."""
        dsm = self.elev.copy()
        if trees_gdf is None or trees_gdf.empty:
            return dsm

        nrows, ncols = dsm.shape
        res_m = self.transform["resolution_m"]
        cell_lat_m = abs(self.transform["cell_lat"]) * 111_320.0
        mid_lat = self.transform["origin_lat"] + nrows / 2 * self.transform["cell_lat"]
        cell_lon_m = self.transform["cell_lon"] * 111_320.0 * np.cos(np.radians(mid_lat))

        for _, row in trees_gdf.iterrows():
            pt = row.geometry
            h = row.get("height_m") or DEFAULT_TREE_HEIGHT_M
            crown_r = float(row.get("canopy_radius_m", 3.0) or 3.0)

            base_r, base_c = latlon_to_grid(pt.y, pt.x, self.transform)
            r_rows = crown_r / cell_lat_m
            r_cols = crown_r / cell_lon_m

            for dr in range(-int(r_rows) - 1, int(r_rows) + 2):
                for dc in range(-int(r_cols) - 1, int(r_cols) + 2):
                    r = int(base_r) + dr
                    c = int(base_c) + dc
                    if 0 <= r < nrows and 0 <= c < ncols:
                        if (dr / max(r_rows, 0.1)) ** 2 + (dc / max(r_cols, 0.1)) ** 2 <= 1.0:
                            dsm[r, c] = max(dsm[r, c], self.elev[r, c] + float(h))

        return dsm

    def _rasterise_buildings(self, buildings_gdf: gpd.GeoDataFrame) -> np.ndarray:
        """Burn building footprints into a boolean mask for fast LOS blocking."""
        mask = np.zeros(self.elev.shape, dtype=bool)
        if buildings_gdf is None or buildings_gdf.empty:
            return mask

        nrows, ncols = self.elev.shape
        for _, row in buildings_gdf.iterrows():
            poly = row.geometry
            minx, miny, maxx, maxy = poly.bounds
            r_min, c_min = latlon_to_grid(maxy, minx, self.transform)
            r_max, c_max = latlon_to_grid(miny, maxx, self.transform)
            for r in range(max(0, int(r_min) - 1), min(nrows, int(r_max) + 2)):
                for c in range(max(0, int(c_min) - 1), min(ncols, int(c_max) + 2)):
                    pt_lat, pt_lon = grid_to_latlon(r, c, self.transform)
                    if poly.contains(Point(pt_lon, pt_lat)):
                        mask[r, c] = True

        return mask

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def get_elevation(self, lat: float, lon: float) -> float:
        """Bare-earth elevation (metres) at a point, bilinearly interpolated."""
        r, c = latlon_to_grid(lat, lon, self.transform)
        r = float(np.clip(r, 0, self.elev.shape[0] - 1))
        c = float(np.clip(c, 0, self.elev.shape[1] - 1))
        return float(scipy.ndimage.map_coordinates(self.elev, [[r], [c]], order=1)[0])

    def shade_fraction(self, lat: float, lon: float, radius_m: float = 50.0) -> float:
        """
        Fraction of sky blocked by nearby tree crowns (solid-angle model).

        Each tree contributes r² / (r² + d²) to the shade sum, so a tree
        directly overhead contributes 1.0 and the contribution falls off
        naturally with distance.  Summed contributions are clipped to 1.0.
        radius_m=50 covers trees that cast meaningful shade at ~15° sun elevation.
        """
        if self.trees_gdf is None or self.trees_gdf.empty:
            return 0.0

        pt = Point(lon, lat)
        rad_deg = radius_m / 111_320.0
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            nearby = self.trees_gdf[self.trees_gdf.geometry.distance(pt) < rad_deg]
        if nearby.empty:
            return 0.0

        m_per_deg_lat = 111_320.0
        m_per_deg_lon = 111_320.0 * np.cos(np.radians(lat))
        shade = 0.0
        for _, row in nearby.iterrows():
            r = float(row.get("canopy_radius_m") or 6.0)
            dlat = (row.geometry.y - lat) * m_per_deg_lat
            dlon = (row.geometry.x - lon) * m_per_deg_lon
            d2 = dlat ** 2 + dlon ** 2
            shade += r * r / (r * r + d2)
        return min(shade, 1.0)

    def batch_los(
        self,
        ap_lat: float,
        ap_lon: float,
        test_lats: np.ndarray,
        test_lons: np.ndarray,
        n_samples: int = 40,
    ) -> tuple[np.ndarray, np.ndarray]:
        """
        Vectorised LOS check from one AP to N test points.

        Returns
        -------
        los_clear   : bool[N]    True when the path is not blocked
        veg_path_m  : float[N]   metres of vegetation along each path
        """
        N = len(test_lats)
        nrows, ncols = self.dsm.shape

        ap_r, ap_c = latlon_to_grid(ap_lat, ap_lon, self.transform)
        ap_elev = float(
            scipy.ndimage.map_coordinates(self.elev, [[ap_r], [ap_c]], order=1)[0]
        )
        ap_z = ap_elev + AP_HEIGHT_M

        test_r, test_c = latlon_to_grid(test_lats, test_lons, self.transform)
        test_elevs = scipy.ndimage.map_coordinates(self.elev, [test_r, test_c], order=1)
        test_z = test_elevs + RX_HEIGHT_M

        # Sample positions along each ray, shape (N, n_samples)
        t = np.linspace(0.0, 1.0, n_samples)
        ray_r = ap_r + t[None, :] * (test_r[:, None] - ap_r)
        ray_c = ap_c + t[None, :] * (test_c[:, None] - ap_c)

        ray_r_cl = np.clip(ray_r, 0, nrows - 1)
        ray_c_cl = np.clip(ray_c, 0, ncols - 1)

        flat_r = ray_r_cl.ravel()
        flat_c = ray_c_cl.ravel()

        dsm_heights = scipy.ndimage.map_coordinates(
            self.dsm, [flat_r, flat_c], order=1
        ).reshape(N, n_samples)

        bare_heights = scipy.ndimage.map_coordinates(
            self.elev, [flat_r, flat_c], order=1
        ).reshape(N, n_samples)

        bldg_hits = scipy.ndimage.map_coordinates(
            self.building_mask.astype(np.float32), [flat_r, flat_c], order=0
        ).reshape(N, n_samples)

        # LOS height at each sample
        z_los = ap_z + t[None, :] * (test_z[:, None] - ap_z)

        # Skip endpoints (t=0, t=1); only intermediate samples can block
        inner = slice(1, -1)
        terrain_blocked = (dsm_heights[:, inner] > z_los[:, inner]).any(axis=1)
        bldg_blocked = (bldg_hits[:, inner] > 0.5).any(axis=1)
        blocked = terrain_blocked | bldg_blocked

        # Vegetation path length: fraction of samples in canopy × total path length
        is_veg = (dsm_heights - bare_heights) > 2.0   # canopy > 2 m above ground

        ap_utm = np.array(to_utm(ap_lon, ap_lat))
        test_utm = np.column_stack(to_utm(test_lons, test_lats))
        path_lengths = np.linalg.norm(test_utm - ap_utm, axis=1)

        veg_path_m = is_veg.mean(axis=1) * path_lengths

        return ~blocked, veg_path_m
