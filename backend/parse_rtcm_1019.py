# parse_rtcm_1019.py
# Robust parser for RTCM 1019 (GPS ephemeris) from pyrtcm message object

import math


def _get_first(msg, names):
    """Return first non-None attribute from msg for given candidate names."""
    for n in names:
        if hasattr(msg, n):
            v = getattr(msg, n)
            if v is not None:
                return v
    return None


def _require(msg, key, names):
    v = _get_first(msg, names)
    if v is None:
        # show some hints for debugging
        attrs = [a for a in dir(msg) if a.startswith("DF") or a.lower() in ("prn", "sat", "identity")]
        attrs = sorted(set(attrs))[:120]
        raise ValueError(
            f"[RTCM1019] Missing field for {key}. Tried: {names}. "
            f"Available attrs sample: {attrs}"
        )
    return v


def parse_rtcm_1019(msg):
    """
    Parse pyrtcm RTCM 1019 message (GPS Ephemeris) into:
    prn, Toe, a_or_sqrtA, e, w0, W0, Wdot, i0, idot, M0, delta_n, Cuc, Cus, Crc, Crs, Cic, Cis

    Returns:
        ("Gxx", Toe, a_or_sqrtA, e, omega, Omega0, OmegaDot, i0, IDOT, M0, delta_n, Cuc, Cus, Crc, Crs, Cic, Cis)

    Notes:
    - Most decoders expose scaled SI units (radians, meters, seconds) already.
    - If your decoder returns semicircles for angles, convert before using in sat pos.
      (pyrtcm usually already returns radians for ephemeris angles; if not, adjust here)
    """

    # --- PRN ---
    prn_id = _require(msg, "PRN", ("DF009", "prn", "PRN", "sat", "SV", "sv"))
    prn = f"G{int(prn_id):02d}"

    # --- Core broadcast ephemeris terms (RINEX-like) ---
    # These candidate names cover common pyrtcm variants and DF numbers that appear in some builds.
    # If your msg uses different DF numbers, the error will show available attrs to map.

    # Toe [s]
    Toe = _require(msg, "Toe", ("Toe", "toe", "TOE", "DF093", "DF097", "DF100"))

    # sqrt(A) [sqrt(m)] or A [m] (we will auto-detect later in satellite_pos)
    sqrtA_or_A = _require(msg, "sqrtA/A", ("sqrtA", "SQRT_A", "A", "a", "DF092", "DF094"))

    # eccentricity
    e = _require(msg, "e", ("e", "Ecc", "ecc", "ECC", "DF090", "DF091"))

    # argument of perigee ω [rad]
    omega = _require(msg, "omega", ("omega", "w", "argPer", "ARGP", "DF099", "DF102"))

    # longitude of ascending node Ω0 [rad]
    Omega0 = _require(msg, "Omega0", ("Omega0", "OMEGA0", "W0", "W_0", "DF095", "DF098"))

    # rate of right ascension Ωdot [rad/s]
    OmegaDot = _require(msg, "OmegaDot", ("OmegaDot", "OMEGADOT", "Wdot", "W_DOT", "DF101", "DF104"))

    # inclination i0 [rad]
    i0 = _require(msg, "i0", ("i0", "I0", "inc0", "DF096", "DF099"))

    # inclination rate IDOT [rad/s]
    IDOT = _require(msg, "IDOT", ("IDOT", "idot", "I_DOT", "DF102", "DF105"))

    # mean anomaly M0 [rad]
    M0 = _require(msg, "M0", ("M0", "M_0", "DF098", "DF101"))

    # mean motion difference Δn [rad/s]
    delta_n = _require(msg, "delta_n", ("DeltaN", "delta_n", "deltan", "DF089", "DF090"))

    # harmonic corrections
    Cuc = _require(msg, "Cuc", ("Cuc", "DF083", "DF084"))
    Cus = _require(msg, "Cus", ("Cus", "DF084", "DF085"))
    Crc = _require(msg, "Crc", ("Crc", "DF091", "DF092"))
    Crs = _require(msg, "Crs", ("Crs", "DF086", "DF087"))
    Cic = _require(msg, "Cic", ("Cic", "DF087", "DF088"))
    Cis = _require(msg, "Cis", ("Cis", "DF088", "DF089"))

    # --- Make sure float ---
    Toe = float(Toe)
    sqrtA_or_A = float(sqrtA_or_A)
    e = float(e)
    omega = float(omega)
    Omega0 = float(Omega0)
    OmegaDot = float(OmegaDot)
    i0 = float(i0)
    IDOT = float(IDOT)
    M0 = float(M0)
    delta_n = float(delta_n)
    Cuc = float(Cuc)
    Cus = float(Cus)
    Crc = float(Crc)
    Crs = float(Crs)
    Cic = float(Cic)
    Cis = float(Cis)

    return (
        prn,
        Toe,
        sqrtA_or_A,
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
    )
