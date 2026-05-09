"""RF propagation model: FSPL + ITU-R P.833 vegetation attenuation."""

import numpy as np

FT_PER_M = 3.28084

# ITU-R P.833 specific attenuation (dB/m through vegetation), in-leaf conditions.
# Higher frequencies suffer more loss per metre of canopy.
_VEG_DB_PER_M: dict[float, float] = {
    2.4: 0.12,   # ~12 dB / 100 m
    5.0: 0.18,   # ~18 dB / 100 m
    6.0: 0.22,   # ~22 dB / 100 m
}

# Default coefficient for any unlisted frequency (interpolate linearly).
_FREQ_SORTED = sorted(_VEG_DB_PER_M)


def _veg_coeff(freq_ghz: float) -> float:
    """Return ITU-R P.833 attenuation coefficient (dB/m) for a given frequency."""
    if freq_ghz <= _FREQ_SORTED[0]:
        return _VEG_DB_PER_M[_FREQ_SORTED[0]]
    if freq_ghz >= _FREQ_SORTED[-1]:
        return _VEG_DB_PER_M[_FREQ_SORTED[-1]]
    for lo, hi in zip(_FREQ_SORTED, _FREQ_SORTED[1:]):
        if lo <= freq_ghz <= hi:
            t = (freq_ghz - lo) / (hi - lo)
            return _VEG_DB_PER_M[lo] + t * (_VEG_DB_PER_M[hi] - _VEG_DB_PER_M[lo])
    return 0.15


def effective_range_m(
    rated_range_m: float,
    freq_ghz: float,
    veg_path_m: float | np.ndarray,
) -> float | np.ndarray:
    """
    Effective range after vegetation attenuation.

    The rated range encodes the full RF link budget in open space.
    Vegetation adds extra path loss A_veg = k * veg_path_m dB on top of FSPL.
    Using the FSPL 20·log10(d) relationship:

        d_eff = rated_range * 10^(-A_veg / 20)

    Works with both scalar and numpy array inputs for veg_path_m.
    """
    k = _veg_coeff(freq_ghz)
    veg_loss_db = k * veg_path_m
    return rated_range_m * (10.0 ** (-veg_loss_db / 20.0))


def effective_range_ft(
    rated_range_ft: float,
    freq_ghz: float,
    veg_path_m: float | np.ndarray,
) -> float | np.ndarray:
    """Convenience wrapper: rated range in feet, returns effective range in feet."""
    rated_m = rated_range_ft / FT_PER_M
    eff_m = effective_range_m(rated_m, freq_ghz, veg_path_m)
    return eff_m * FT_PER_M
