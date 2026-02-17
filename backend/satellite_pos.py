# satellite_pos.py
# Compute GNSS satellite ECEF position from broadcast ephemeris

import math

# Gravitational constants (m^3/s^2)
MU_GPS = 3.986005e14
MU_GAL = 3.986004418e14
MU_BDS = 3.986004418e14
MU_GLO = 3.9860044e14

# Earth rotation rates (rad/s)
OMEGA_E_GPS = 7.2921151467e-5
OMEGA_E_GAL = 7.2921151467e-5
OMEGA_E_BDS = 7.292115e-5
OMEGA_E_GLO = 7.292115e-5

# Backward-compatible names (legacy GPS-only code)
MU = MU_GPS
OMEGA_E = OMEGA_E_GPS

HALF_WEEK = 302400.0
WEEK = 604800.0
HALF_DAY = 43200.0
DAY = 86400.0

# GLONASS constants
J2_GLO = 1.0826257e-3
AE_GLO = 6378136.0


def _wrap_week(tk: float) -> float:
    if tk > HALF_WEEK:
        tk -= WEEK
    elif tk < -HALF_WEEK:
        tk += WEEK
    return tk


def _wrap_day(tk: float) -> float:
    if tk > HALF_DAY:
        tk -= DAY
    elif tk < -HALF_DAY:
        tk += DAY
    return tk


def _kepler_E(M: float, e: float, iters: int = 10) -> float:
    E = M
    for _ in range(iters):
        E = E + (M - (E - e * math.sin(E))) / (1.0 - e * math.cos(E))
    return E


def calculate_sat_pos_kepler(
    t: float,
    toe: float,
    a_or_sqrtA: float,
    e: float,
    omega: float,
    Omega0: float,
    OmegaDot: float,
    i0: float,
    IDOT: float,
    M0: float,
    delta_n: float,
    Cuc: float,
    Cus: float,
    Crc: float,
    Crs: float,
    Cic: float,
    Cis: float,
    *,
    mu: float = MU_GPS,
    omega_e: float = OMEGA_E_GPS,
):
    """
    Generic Keplerian GNSS satellite position (ECEF) from broadcast ephemeris.
    Works for GPS/QZSS/GAL/BDS when used with correct mu and omega_e.
    """
    # 1) semi-major axis
    if a_or_sqrtA < 1e6:
        A = a_or_sqrtA * a_or_sqrtA
    else:
        A = a_or_sqrtA

    # 2) time from ephemeris reference
    tk = _wrap_week(float(t) - float(toe))

    # 3) mean motion
    n0 = math.sqrt(mu / (A ** 3))
    n = n0 + float(delta_n)

    # 4) mean anomaly
    M = float(M0) + n * tk

    # 5) eccentric anomaly
    E = _kepler_E(M, float(e), iters=12)

    sinE = math.sin(E)
    cosE = math.cos(E)

    # 6) true anomaly
    sqrt1e2 = math.sqrt(max(0.0, 1.0 - e * e))
    v = math.atan2(sqrt1e2 * sinE, cosE - e)

    # 7) argument of latitude
    phi = v + float(omega)

    # 8) harmonic perturbations
    two_phi = 2.0 * phi
    du = float(Cus) * math.sin(two_phi) + float(Cuc) * math.cos(two_phi)
    dr = float(Crs) * math.sin(two_phi) + float(Crc) * math.cos(two_phi)
    di = float(Cis) * math.sin(two_phi) + float(Cic) * math.cos(two_phi)

    # 9) corrected parameters
    u = phi + du
    r = A * (1.0 - e * cosE) + dr
    i = float(i0) + di + float(IDOT) * tk

    # 10) position in orbital plane
    x_orb = r * math.cos(u)
    y_orb = r * math.sin(u)

    # 11) corrected longitude of ascending node
    Omega = float(Omega0) + (float(OmegaDot) - omega_e) * tk - omega_e * float(toe)

    cosO = math.cos(Omega)
    sinO = math.sin(Omega)
    cosi = math.cos(i)
    sini = math.sin(i)

    # 12) ECEF coordinates
    x = x_orb * cosO - y_orb * cosi * sinO
    y = x_orb * sinO + y_orb * cosi * cosO
    z = y_orb * sini

    return x, y, z


def calculate_sat_pos(
    t: float,
    toe: float,
    a_or_sqrtA: float,
    e: float,
    omega: float,
    Omega0: float,
    OmegaDot: float,
    i0: float,
    IDOT: float,
    M0: float,
    delta_n: float,
    Cuc: float,
    Cus: float,
    Crc: float,
    Crs: float,
    Cic: float,
    Cis: float,
):
    """
    Backward-compatible GPS helper.
    """
    return calculate_sat_pos_kepler(
        t,
        toe,
        a_or_sqrtA,
        e,
        omega,
        Omega0,
        OmegaDot,
        i0,
        IDOT,
        M0,
        delta_n,
        Cuc,
        Cus,
        Crc,
        Crs,
        Cic,
        Cis,
        mu=MU_GPS,
        omega_e=OMEGA_E_GPS,
    )


def _glonass_accel(x, y, z, vx, vy, vz, ax, ay, az):
    r2 = x * x + y * y + z * z
    r = math.sqrt(r2)
    if r == 0:
        return 0.0, 0.0, 0.0

    # central gravity
    a0 = -MU_GLO / (r ** 3)

    # J2 perturbation
    z2 = z * z
    r5 = r2 * r2 * r
    k = 1.5 * J2_GLO * MU_GLO * (AE_GLO ** 2) / r5
    f = 1.0 - 5.0 * z2 / r2

    ax_g = a0 * x + k * x * f
    ay_g = a0 * y + k * y * f
    az_g = a0 * z + k * z * (3.0 - 5.0 * z2 / r2)

    # Earth rotation terms in ECEF
    omega = OMEGA_E_GLO
    ax_rot = (omega ** 2) * x + 2.0 * omega * vy
    ay_rot = (omega ** 2) * y - 2.0 * omega * vx

    return (
        ax_g + ax_rot + ax,
        ay_g + ay_rot + ay,
        az_g + az,
    )


def _rk4_step(state, dt, ax, ay, az):
    x, y, z, vx, vy, vz = state

    def deriv(s):
        sx, sy, sz, svx, svy, svz = s
        ax_t, ay_t, az_t = _glonass_accel(sx, sy, sz, svx, svy, svz, ax, ay, az)
        return (svx, svy, svz, ax_t, ay_t, az_t)

    k1 = deriv((x, y, z, vx, vy, vz))
    k2 = deriv((
        x + 0.5 * dt * k1[0],
        y + 0.5 * dt * k1[1],
        z + 0.5 * dt * k1[2],
        vx + 0.5 * dt * k1[3],
        vy + 0.5 * dt * k1[4],
        vz + 0.5 * dt * k1[5],
    ))
    k3 = deriv((
        x + 0.5 * dt * k2[0],
        y + 0.5 * dt * k2[1],
        z + 0.5 * dt * k2[2],
        vx + 0.5 * dt * k2[3],
        vy + 0.5 * dt * k2[4],
        vz + 0.5 * dt * k2[5],
    ))
    k4 = deriv((
        x + dt * k3[0],
        y + dt * k3[1],
        z + dt * k3[2],
        vx + dt * k3[3],
        vy + dt * k3[4],
        vz + dt * k3[5],
    ))

    x += (dt / 6.0) * (k1[0] + 2.0 * k2[0] + 2.0 * k3[0] + k4[0])
    y += (dt / 6.0) * (k1[1] + 2.0 * k2[1] + 2.0 * k3[1] + k4[1])
    z += (dt / 6.0) * (k1[2] + 2.0 * k2[2] + 2.0 * k3[2] + k4[2])
    vx += (dt / 6.0) * (k1[3] + 2.0 * k2[3] + 2.0 * k3[3] + k4[3])
    vy += (dt / 6.0) * (k1[4] + 2.0 * k2[4] + 2.0 * k3[4] + k4[4])
    vz += (dt / 6.0) * (k1[5] + 2.0 * k2[5] + 2.0 * k3[5] + k4[5])

    return (x, y, z, vx, vy, vz)


def calculate_sat_pos_glonass(t, tb, x, y, z, vx, vy, vz, ax, ay, az):
    """
    GLONASS broadcast ephemeris propagation (ECEF) using RK4.
    Inputs x,y,z (m), vx,vy,vz (m/s), ax,ay,az (m/s^2) at tb (s, GLONASS time of day).
    """
    dt = _wrap_day(float(t) - float(tb))
    if abs(dt) < 1e-6:
        return float(x), float(y), float(z)

    step = 60.0
    h = step if dt > 0 else -step
    n = int(abs(dt) // step)

    state = (float(x), float(y), float(z), float(vx), float(vy), float(vz))
    for _ in range(n):
        state = _rk4_step(state, h, float(ax), float(ay), float(az))

    rem = dt - n * h
    if abs(rem) > 1e-6:
        state = _rk4_step(state, rem, float(ax), float(ay), float(az))

    return state[0], state[1], state[2]
