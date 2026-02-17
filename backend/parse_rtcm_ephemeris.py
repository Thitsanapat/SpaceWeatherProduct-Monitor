# backend/parse_rtcm_ephemeris.py
# -*- coding: utf-8 -*-
"""
Parsers for RTCM ephemeris messages (non-GPS):
- 1020 GLONASS
- 1042 BeiDou
- 1044 QZSS
- 1045/1046 Galileo
"""


def _get_first(msg, names):
    for n in names:
        if hasattr(msg, n):
            v = getattr(msg, n)
            if v is not None:
                return v
    return None


def _require(msg, key, names):
    v = _get_first(msg, names)
    if v is None:
        attrs = [a for a in dir(msg) if a.startswith("DF") or a.lower() in ("prn", "sat", "identity")]
        attrs = sorted(set(attrs))[:120]
        raise ValueError(
            f"[RTCM] Missing field for {key}. Tried: {names}. "
            f"Available attrs sample: {attrs}"
        )
    return v


def parse_rtcm_1042(msg):
    prn_id = _require(msg, "PRN", ("DF488", "prn", "PRN", "sat", "SV", "sv"))
    prn = f"C{int(prn_id):02d}"

    toe = float(_require(msg, "Toe", ("DF505", "toe", "TOE")))
    sqrtA = float(_require(msg, "sqrtA", ("DF504", "sqrtA", "A", "a")))
    e = float(_require(msg, "e", ("DF502", "e", "Ecc")))
    omega = float(_require(msg, "omega", ("DF511", "omega", "w")))
    Omega0 = float(_require(msg, "Omega0", ("DF507", "Omega0", "W0")))
    OmegaDot = float(_require(msg, "OmegaDot", ("DF512", "OmegaDot", "Wdot")))
    i0 = float(_require(msg, "i0", ("DF509", "i0")))
    IDOT = float(_require(msg, "IDOT", ("DF491", "IDOT")))
    M0 = float(_require(msg, "M0", ("DF500", "M0")))
    delta_n = float(_require(msg, "delta_n", ("DF499", "DeltaN", "delta_n")))
    Cuc = float(_require(msg, "Cuc", ("DF501", "Cuc")))
    Cus = float(_require(msg, "Cus", ("DF503", "Cus")))
    Crc = float(_require(msg, "Crc", ("DF510", "Crc")))
    Crs = float(_require(msg, "Crs", ("DF498", "Crs")))
    Cic = float(_require(msg, "Cic", ("DF506", "Cic")))
    Cis = float(_require(msg, "Cis", ("DF508", "Cis")))

    return (
        prn,
        toe,
        sqrtA,
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


def parse_rtcm_1044(msg):
    prn_id = _require(msg, "PRN", ("DF429", "prn", "PRN", "sat", "SV", "sv"))
    prn = f"J{int(prn_id):02d}"

    toe = float(_require(msg, "Toe", ("DF442", "toe", "TOE")))
    sqrtA = float(_require(msg, "sqrtA", ("DF441", "sqrtA", "A", "a")))
    e = float(_require(msg, "e", ("DF439", "e", "Ecc")))
    omega = float(_require(msg, "omega", ("DF448", "omega", "w")))
    Omega0 = float(_require(msg, "Omega0", ("DF444", "Omega0", "W0")))
    OmegaDot = float(_require(msg, "OmegaDot", ("DF449", "OmegaDot", "Wdot")))
    i0 = float(_require(msg, "i0", ("DF446", "i0")))
    IDOT = float(_require(msg, "IDOT", ("DF450", "IDOT")))
    M0 = float(_require(msg, "M0", ("DF437", "M0")))
    delta_n = float(_require(msg, "delta_n", ("DF436", "DeltaN", "delta_n")))
    Cuc = float(_require(msg, "Cuc", ("DF438", "Cuc")))
    Cus = float(_require(msg, "Cus", ("DF440", "Cus")))
    Crc = float(_require(msg, "Crc", ("DF447", "Crc")))
    Crs = float(_require(msg, "Crs", ("DF435", "Crs")))
    Cic = float(_require(msg, "Cic", ("DF443", "Cic")))
    Cis = float(_require(msg, "Cis", ("DF445", "Cis")))

    return (
        prn,
        toe,
        sqrtA,
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


def parse_rtcm_1045_1046(msg):
    prn_id = _require(msg, "PRN", ("DF252", "prn", "PRN", "sat", "SV", "sv"))
    prn = f"E{int(prn_id):02d}"

    toe = float(_require(msg, "Toe", ("DF304", "toe", "TOE")))
    sqrtA = float(_require(msg, "sqrtA", ("DF303", "sqrtA", "A", "a")))
    e = float(_require(msg, "e", ("DF301", "e", "Ecc")))
    omega = float(_require(msg, "omega", ("DF310", "omega", "w")))
    Omega0 = float(_require(msg, "Omega0", ("DF306", "Omega0", "W0")))
    OmegaDot = float(_require(msg, "OmegaDot", ("DF311", "OmegaDot", "Wdot")))
    i0 = float(_require(msg, "i0", ("DF308", "i0")))
    IDOT = float(_require(msg, "IDOT", ("DF292", "IDOT")))
    M0 = float(_require(msg, "M0", ("DF299", "M0")))
    delta_n = float(_require(msg, "delta_n", ("DF298", "DeltaN", "delta_n")))
    Cuc = float(_require(msg, "Cuc", ("DF300", "Cuc")))
    Cus = float(_require(msg, "Cus", ("DF302", "Cus")))
    Crc = float(_require(msg, "Crc", ("DF309", "Crc")))
    Crs = float(_require(msg, "Crs", ("DF297", "Crs")))
    Cic = float(_require(msg, "Cic", ("DF305", "Cic")))
    Cis = float(_require(msg, "Cis", ("DF307", "Cis")))

    return (
        prn,
        toe,
        sqrtA,
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


def parse_rtcm_1020(msg):
    prn_id = _require(msg, "PRN", ("DF038", "prn", "PRN", "sat", "SV", "sv"))
    prn = f"R{int(prn_id):02d}"

    tb_raw = float(_require(msg, "tb", ("DF110", "tb", "TB")))
    # DF110 is 7-bit, typically 15-minute intervals
    tb = tb_raw * 900.0

    x = float(_require(msg, "x", ("DF112", "X")))
    y = float(_require(msg, "y", ("DF115", "Y")))
    z = float(_require(msg, "z", ("DF118", "Z")))
    vx = float(_require(msg, "vx", ("DF111", "VX")))
    vy = float(_require(msg, "vy", ("DF114", "VY")))
    vz = float(_require(msg, "vz", ("DF117", "VZ")))
    ax = float(_require(msg, "ax", ("DF113", "AX")))
    ay = float(_require(msg, "ay", ("DF116", "AY")))
    az = float(_require(msg, "az", ("DF119", "AZ")))

    # pyrtcm scales to km / km/s / km/s^2 -> convert to meters
    x *= 1000.0
    y *= 1000.0
    z *= 1000.0
    vx *= 1000.0
    vy *= 1000.0
    vz *= 1000.0
    ax *= 1000.0
    ay *= 1000.0
    az *= 1000.0

    return prn, tb, x, y, z, vx, vy, vz, ax, ay, az
