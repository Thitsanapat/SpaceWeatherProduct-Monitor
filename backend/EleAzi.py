# backend/EleAzi.py
# -*- coding: utf-8 -*-
"""
WGS84 helpers:
- lla2ecef(lat, lon, alt_m) -> (x,y,z) [meters]
- ecef2lla(x,y,z) -> (lat_deg, lon_deg, alt_m)
- ecef_to_enu(dx,dy,dz, lat0, lon0) -> (e,n,u)
- calculate_el_az(sat_xyz, rx_xyz, lat0, lon0) -> (el_deg, az_deg)
- calculate_el(...) -> el_deg  (compat with your existing calls)
"""

import numpy as np

# WGS84 constants
_A = 6378137.0                     # semi-major axis (m)
_F = 1.0 / 298.257223563           # flattening
_E2 = _F * (2.0 - _F)              # first eccentricity squared
_B = _A * (1.0 - _F)               # semi-minor axis (m)
_EP2 = (_A**2 - _B**2) / _B**2     # second eccentricity squared


def lla2ecef(lat_deg: float, lon_deg: float, alt_m: float):
    """Convert geodetic LLA (deg,deg,m) to ECEF XYZ (m)."""
    lat = np.deg2rad(lat_deg)
    lon = np.deg2rad(lon_deg)

    sin_lat = np.sin(lat)
    cos_lat = np.cos(lat)
    sin_lon = np.sin(lon)
    cos_lon = np.cos(lon)

    N = _A / np.sqrt(1.0 - _E2 * sin_lat**2)

    x = (N + alt_m) * cos_lat * cos_lon
    y = (N + alt_m) * cos_lat * sin_lon
    z = (N * (1.0 - _E2) + alt_m) * sin_lat
    return float(x), float(y), float(z)


def ecef2lla(x: float, y: float, z: float):
    """
    Convert ECEF XYZ (m) to geodetic LLA (deg,deg,m).
    Uses Bowring's method (stable and fast).
    """
    x = float(x); y = float(y); z = float(z)
    p = np.sqrt(x*x + y*y)

    # lon
    lon = np.arctan2(y, x)

    # initial lat
    th = np.arctan2(_A * z, _B * p)
    sin_th = np.sin(th)
    cos_th = np.cos(th)

    lat = np.arctan2(
        z + _EP2 * _B * sin_th**3,
        p - _E2 * _A * cos_th**3
    )

    sin_lat = np.sin(lat)
    N = _A / np.sqrt(1.0 - _E2 * sin_lat**2)
    alt = p / np.cos(lat) - N

    lat_deg = float(np.rad2deg(lat))
    lon_deg = float(np.rad2deg(lon))
    # wrap lon to [-180, 180]
    lon_deg = (lon_deg + 540.0) % 360.0 - 180.0

    return lat_deg, lon_deg, float(alt)


def _R_ecef_to_enu(lat_deg: float, lon_deg: float):
    """Rotation matrix from ECEF to ENU at reference lat/lon."""
    lat = np.deg2rad(lat_deg)
    lon = np.deg2rad(lon_deg)

    sin_lat = np.sin(lat)
    cos_lat = np.cos(lat)
    sin_lon = np.sin(lon)
    cos_lon = np.cos(lon)

    # ECEF -> ENU
    return np.array([
        [-sin_lon,            cos_lon,           0.0],
        [-sin_lat*cos_lon, -sin_lat*sin_lon,  cos_lat],
        [ cos_lat*cos_lon,  cos_lat*sin_lon,  sin_lat],
    ], dtype=float)


def ecef_to_enu(dx: float, dy: float, dz: float, lat0_deg: float, lon0_deg: float):
    """
    Convert delta vector (sat - rx) in ECEF to ENU components at (lat0, lon0).
    Returns (E, N, U) in meters.
    """
    R = _R_ecef_to_enu(lat0_deg, lon0_deg)
    enu = R @ np.array([dx, dy, dz], dtype=float)
    return float(enu[0]), float(enu[1]), float(enu[2])


def calculate_el_az(sat_x, sat_y, sat_z, rx_x, rx_y, rx_z, rx_lat_deg, rx_lon_deg):
    """
    Compute elevation/azimuth (deg) of satellite given:
    - satellite ECEF (m)
    - receiver ECEF (m)
    - receiver lat/lon (deg)
    """
    dx = float(sat_x) - float(rx_x)
    dy = float(sat_y) - float(rx_y)
    dz = float(sat_z) - float(rx_z)

    e, n, u = ecef_to_enu(dx, dy, dz, rx_lat_deg, rx_lon_deg)

    # elevation
    horiz = np.sqrt(e*e + n*n)
    el = np.rad2deg(np.arctan2(u, horiz))

    # azimuth: atan2(E, N) then wrap to [0,360)
    az = np.rad2deg(np.arctan2(e, n))
    az = (az + 360.0) % 360.0

    return float(el), float(az)


def calculate_el(sat_x, sat_y, sat_z, u_x, u_y, u_z, u_lat, u_lon):
    """
    Backward-compatible helper: returns elevation only (deg).
    Matches your existing call in worker.
    """
    el, _az = calculate_el_az(sat_x, sat_y, sat_z, u_x, u_y, u_z, u_lat, u_lon)
    # round to 0.1 deg like your old code
    return float(f"{el:.1f}")
