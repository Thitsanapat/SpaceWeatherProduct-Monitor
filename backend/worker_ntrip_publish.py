# backend/worker_ntrip_publish.py
"""
NTRIP worker:
- connect NTRIP caster
- parse RTCM MSM7 multi-GNSS (GPS/GLO/GAL/BDS/QZSS/IRNSS) excluding SBAS
- compute S4c/ROTI/VTEC per PRN
- publish to backend /api/publish
"""

import time
import json
import os
import sys
import queue
import threading
import csv
from collections import defaultdict, deque
from datetime import datetime, timezone

import numpy as np
import requests
import pynmea2
from pyrtcm import RTCMReader
from pyrtcm.rtcmhelpers import parse_msm

try:
    import client
    import EleAzi
    from parse_rtcm_1019 import parse_rtcm_1019
    from parse_rtcm_ephemeris import (
        parse_rtcm_1020,
        parse_rtcm_1042,
        parse_rtcm_1044,
        parse_rtcm_1045_1046,
    )
    from satellite_pos import (
        MU_GAL,
        MU_BDS,
        MU_GPS,
        OMEGA_E_GAL,
        OMEGA_E_BDS,
        OMEGA_E_GPS,
        calculate_sat_pos,
        calculate_sat_pos_kepler,
        calculate_sat_pos_glonass,
    )
    from stations import DEFAULT_STATION_ID, get_station, list_station_ids
except Exception:  # pragma: no cover
    from backend import client, EleAzi
    from backend.parse_rtcm_1019 import parse_rtcm_1019
    from backend.parse_rtcm_ephemeris import (
        parse_rtcm_1020,
        parse_rtcm_1042,
        parse_rtcm_1044,
        parse_rtcm_1045_1046,
    )
    from backend.satellite_pos import (
        MU_GAL,
        MU_BDS,
        MU_GPS,
        OMEGA_E_GAL,
        OMEGA_E_BDS,
        OMEGA_E_GPS,
        calculate_sat_pos,
        calculate_sat_pos_kepler,
        calculate_sat_pos_glonass,
    )
    from backend.stations import DEFAULT_STATION_ID, get_station, list_station_ids


# -------------------- config --------------------
BACKEND_PUBLISH = "http://localhost:8000/api/publish"

CASTER, PORT = "161.246.18.204", 2101
USER, PASSWORD = "tele4", "cssrg1234"

# choose station from CLI arg or WORKER_STATION env var, default to KMIT6
if len(sys.argv) > 1:
    STATION_ID = sys.argv[1].upper()
else:
    STATION_ID = os.environ.get("WORKER_STATION", DEFAULT_STATION_ID).upper()

station_cfg = get_station(STATION_ID)
if station_cfg is None:
    known = ", ".join(list_station_ids())
    print(f"Warning: station {STATION_ID} not in stations list ({known}), defaulting to {DEFAULT_STATION_ID}")
    STATION_ID = DEFAULT_STATION_ID
    station_cfg = get_station(STATION_ID)

LAT, LON, ALT = station_cfg["lat"], station_cfg["lon"], station_cfg["alt"]
MOUNT = os.environ.get("WORKER_MOUNT", station_cfg.get("mount") or station_cfg["id"])
ELEV_CUT = 30.0

WIN_CNO, WIN_STEC = 20, 60
SLIP_THR = 0.5  # TECU jump threshold
C_LIGHT = 299792458.0  # m/s
RE_KM = 6371.0
IONO_H_KM = 350.0

# TEC const (GPS L1/L2)
F1, F2 = 1575.42e6, 1227.60e6
TEC_K = (F1**2 * F2**2) / (40.3 * (F1**2 - F2**2)) * 1e-16  # TECU / m

# MSM7 identities (multi-GNSS)
MSM7_IDS = {"1077", "1087", "1097", "1117", "1127", "1137", "1107"}  # 1107=SBAS (skip)
GNSS_PREFIX = {
    "GPS": "G",
    "GLONASS": "R",
    "GALILEO": "E",
    "BEIDOU": "C",
    "QZSS": "J",
    "NAVIC": "I",
    "IRNSS": "I",
}

# carrier frequencies (Hz)
FREQ_MAP = {
    "GPS": {"L1": 1575.42e6, "L2": 1227.60e6, "L5": 1176.45e6},
    "QZSS": {"L1": 1575.42e6, "L2": 1227.60e6, "L5": 1176.45e6, "LEX": 1278.75e6},
    "GALILEO": {"E1": 1575.42e6, "E5A": 1176.45e6, "E5B": 1207.14e6, "E5AB": 1191.795e6, "E6": 1278.75e6},
    "BEIDOU": {"B1": 1561.098e6, "B1C": 1575.42e6, "B2": 1207.14e6, "B2A": 1176.45e6, "B2B": 1207.14e6, "B3": 1268.52e6},
    "NAVIC": {"L5": 1176.45e6, "S": 2492.028e6},
    "IRNSS": {"L5": 1176.45e6, "S": 2492.028e6},
}

CN0_PRIORITY = {
    "GPS": ["L1", "L2", "L5"],
    "QZSS": ["L1", "L2", "L5", "LEX"],
    "GLONASS": ["G1", "G2"],
    "GALILEO": ["E1", "E5A", "E5B", "E5AB", "E6"],
    "BEIDOU": ["B1", "B1C", "B2", "B2A", "B2B", "B3"],
    "NAVIC": ["L5", "S"],
    "IRNSS": ["L5", "S"],
}

TEC_PAIR_PRIORITY = {
    "GPS": [("L1", "L2"), ("L1", "L5"), ("L2", "L5")],
    "QZSS": [("L1", "L2"), ("L1", "L5"), ("L2", "L5")],
    "GLONASS": [("G1", "G2")],
    "GALILEO": [("E1", "E5A"), ("E1", "E5B"), ("E1", "E5AB"), ("E5A", "E5B"), ("E1", "E6")],
    "BEIDOU": [("B1", "B2"), ("B1", "B3"), ("B1C", "B2A"), ("B1C", "B2B"), ("B1", "B2A"), ("B1", "B2B"), ("B1C", "B3")],
    "NAVIC": [("L5", "S")],
    "IRNSS": [("L5", "S")],
}


UTC = timezone.utc
def now_utc():
    return datetime.now(UTC)

def is_allowed_prn(prn: str) -> bool:
    if not prn:
        return False
    return prn[0] in ("G", "E", "C", "R", "J", "I")  # no S, no other

def deg2dm_str(deg, is_lat=True):
    sign = 1 if deg >= 0 else -1
    d = int(abs(deg)); m = (abs(deg)-d) * 60
    return (f"{sign*d:02d}" if is_lat else f"{sign*d:03d}") + f"{m:07.4f}"

def s4c(vals):
    a = np.array(vals, dtype=float)
    if a.size == 0:
        return np.nan
    mu = a.mean()
    return np.sqrt(((a - mu) ** 2).mean()) / mu if mu else np.nan


def fmt_prn(gnss: str, prn_str: str):
    prefix = GNSS_PREFIX.get(gnss.upper())
    if not prefix:
        return None
    try:
        n = int(prn_str)
    except Exception:
        return None
    width = 2 if n < 100 else 3
    return f"{prefix}{n:0{width}d}"


def glonass_channel(raw):
    if raw is None:
        return None
    try:
        k = int(raw)
    except Exception:
        return None
    # DF419 is often encoded as 0..13 for channels -7..+6.
    if 0 <= k <= 13:
        k = k - 7
    if k < -7 or k > 6:
        return None
    return k


def get_freq(gnss: str, band: str, k_glonass=None):
    g = gnss.upper()
    b = band.upper()
    if g == "GLONASS":
        if k_glonass is None:
            return None
        if b == "G1":
            return (1602.0 + 0.5625 * k_glonass) * 1e6
        if b == "G2":
            return (1246.0 + 0.4375 * k_glonass) * 1e6
        return None
    return FREQ_MAP.get(g, {}).get(b)


def pick_cn0(gnss: str, sigs: dict):
    pref = CN0_PRIORITY.get(gnss.upper(), [])
    for band in pref:
        d = sigs.get(band)
        if d and d.get("cn0") is not None:
            return d["cn0"]
    vals = [d.get("cn0") for d in sigs.values() if d.get("cn0") is not None]
    return max(vals) if vals else None


def pick_pair(gnss: str, sigs: dict, key: str = "phase"):
    avail = {
        b for b, d in sigs.items()
        if d.get(key) is not None and np.isfinite(d.get(key))
    }
    for a, b in TEC_PAIR_PRIORITY.get(gnss.upper(), []):
        if a in avail and b in avail:
            return (a, b)
    return None


def compute_stec(gnss: str, sigs: dict, k_glonass=None, key: str = "phase"):
    pair = pick_pair(gnss, sigs, key=key)
    if not pair:
        return None
    b1, b2 = pair
    p1 = sigs[b1].get(key)
    p2 = sigs[b2].get(key)
    if p1 is None or p2 is None:
        return None
    if not np.isfinite(p1) or not np.isfinite(p2):
        return None
    f1 = get_freq(gnss, b1, k_glonass)
    f2 = get_freq(gnss, b2, k_glonass)
    if not f1 or not f2 or f1 == f2:
        return None
    den = 40.3 * ((1.0 / (f2 ** 2)) - (1.0 / (f1 ** 2)))
    if den == 0:
        return None
    return float((p2 - p1) / den * 1e-16)


def slant_factor(el_deg: float):
    el = np.radians(el_deg)
    x = (RE_KM * np.cos(el) / (RE_KM + IONO_H_KM)) ** 2
    return 1.0 / np.sqrt(max(1.0 - x, 1e-12))


# -------------------- state --------------------
cn0_buf  = defaultdict(lambda: deque(maxlen=WIN_CNO))   # per-PRN
stec_buf = defaultdict(lambda: deque(maxlen=WIN_STEC))  # per-PRN
eph_gps = {}
eph_gal = {}
eph_bds = {}
eph_qzs = {}
eph_glo = {}
seen_msm_gps = False

publish_q: "queue.Queue[dict]" = queue.Queue()


# -------------------- sat elevation --------------------
def elev_deg(prn: str, tow: float):
    if prn not in eph_gps:
        return None
    e = eph_gps[prn]
    x, y, z = calculate_sat_pos(
        tow, e["Toe"], e["a"], e["e"], e["w0"], e["W0"], e["Wdot"],
        e["i0"], e["idot"], e["M0"], e["delta_n"],
        e["Cuc"], e["Cus"], e["Crc"], e["Crs"], e["Cic"], e["Cis"]
    )
    ux, uy, uz = EleAzi.lla2ecef(LAT, LON, ALT)
    return EleAzi.calculate_el(x, y, z, ux, uy, uz, LAT, LON)


def elev_deg_any(gnss: str, prn: str, t_sec: float):
    ux, uy, uz = EleAzi.lla2ecef(LAT, LON, ALT)

    g = gnss.upper()
    if g == "GPS":
        return elev_deg(prn, t_sec)
    if g == "GALILEO":
        e = eph_gal.get(prn)
        if not e:
            return None
        x, y, z = calculate_sat_pos_kepler(
            t_sec, e["Toe"], e["a"], e["e"], e["w0"], e["W0"], e["Wdot"],
            e["i0"], e["idot"], e["M0"], e["delta_n"],
            e["Cuc"], e["Cus"], e["Crc"], e["Crs"], e["Cic"], e["Cis"],
            mu=MU_GAL, omega_e=OMEGA_E_GAL,
        )
        return EleAzi.calculate_el(x, y, z, ux, uy, uz, LAT, LON)
    if g == "BEIDOU":
        e = eph_bds.get(prn)
        if not e:
            return None
        x, y, z = calculate_sat_pos_kepler(
            t_sec, e["Toe"], e["a"], e["e"], e["w0"], e["W0"], e["Wdot"],
            e["i0"], e["idot"], e["M0"], e["delta_n"],
            e["Cuc"], e["Cus"], e["Crc"], e["Crs"], e["Cic"], e["Cis"],
            mu=MU_BDS, omega_e=OMEGA_E_BDS,
        )
        return EleAzi.calculate_el(x, y, z, ux, uy, uz, LAT, LON)
    if g == "QZSS":
        e = eph_qzs.get(prn)
        if not e:
            return None
        x, y, z = calculate_sat_pos_kepler(
            t_sec, e["Toe"], e["a"], e["e"], e["w0"], e["W0"], e["Wdot"],
            e["i0"], e["idot"], e["M0"], e["delta_n"],
            e["Cuc"], e["Cus"], e["Crc"], e["Crs"], e["Cic"], e["Cis"],
            mu=MU_GPS, omega_e=OMEGA_E_GPS,
        )
        return EleAzi.calculate_el(x, y, z, ux, uy, uz, LAT, LON)
    if g == "GLONASS":
        e = eph_glo.get(prn)
        if not e:
            return None
        x, y, z = calculate_sat_pos_glonass(
            t_sec, e["tb"], e["x"], e["y"], e["z"], e["vx"], e["vy"], e["vz"], e["ax"], e["ay"], e["az"]
        )
        return EleAzi.calculate_el(x, y, z, ux, uy, uz, LAT, LON)

    return None


# -------------------- publish worker --------------------
def publish_loop():
    buf = []
    last_flush = time.time()

    while True:
        try:
            item = publish_q.get(timeout=0.2)
            buf.append(item)
        except queue.Empty:
            pass

        # flush every 0.5s or when enough
        if buf and (len(buf) >= 20 or (time.time() - last_flush) > 0.5):
            payload = {"station": STATION_ID, "data": buf}
            try:
                requests.post(BACKEND_PUBLISH, json=payload, timeout=2.0)
            except Exception:
                # network/backend down -> keep worker alive
                pass
            buf = []
            last_flush = time.time()


# -------------------- NTRIP --------------------
def make_gga_sentence(sock):
    while True:
        gga = pynmea2.GGA('GP', 'GGA', (
            now_utc().strftime("%H%M%S"),
            deg2dm_str(LAT, True),  'N' if LAT >= 0 else 'S',
            deg2dm_str(LON, False), 'E' if LON >= 0 else 'W',
            '1', '06', '1.0', f"{ALT:.1f}", 'M', '', '', ''
        ))
        try:
            sock.sendall((str(gga) + "\r\n").encode())
        except Exception:
            break
        time.sleep(15)


# -------------------- RTCM handlers --------------------
def handle_1019(msg):
    prn, *v = parse_rtcm_1019(msg)
    eph_gps[prn] = dict(
        Toe=v[0], a=v[1], e=v[2], w0=v[3], W0=v[4], Wdot=v[5],
        i0=v[6], idot=v[7], M0=v[8], delta_n=v[9],
        Cuc=v[10], Cus=v[11], Crc=v[12], Crs=v[13],
        Cic=v[14], Cis=v[15],
    )


def handle_1045_1046(msg):
    prn, *v = parse_rtcm_1045_1046(msg)
    eph_gal[prn] = dict(
        Toe=v[0], a=v[1], e=v[2], w0=v[3], W0=v[4], Wdot=v[5],
        i0=v[6], idot=v[7], M0=v[8], delta_n=v[9],
        Cuc=v[10], Cus=v[11], Crc=v[12], Crs=v[13],
        Cic=v[14], Cis=v[15],
    )


def handle_1042(msg):
    prn, *v = parse_rtcm_1042(msg)
    eph_bds[prn] = dict(
        Toe=v[0], a=v[1], e=v[2], w0=v[3], W0=v[4], Wdot=v[5],
        i0=v[6], idot=v[7], M0=v[8], delta_n=v[9],
        Cuc=v[10], Cus=v[11], Crc=v[12], Crs=v[13],
        Cic=v[14], Cis=v[15],
    )


def handle_1044(msg):
    prn, *v = parse_rtcm_1044(msg)
    eph_qzs[prn] = dict(
        Toe=v[0], a=v[1], e=v[2], w0=v[3], W0=v[4], Wdot=v[5],
        i0=v[6], idot=v[7], M0=v[8], delta_n=v[9],
        Cuc=v[10], Cus=v[11], Crc=v[12], Crs=v[13],
        Cic=v[14], Cis=v[15],
    )


def handle_1020(msg):
    prn, tb, x, y, z, vx, vy, vz, ax, ay, az = parse_rtcm_1020(msg)
    eph_glo[prn] = dict(tb=tb, x=x, y=y, z=z, vx=vx, vy=vy, vz=vz, ax=ax, ay=ay, az=az)


def handle_msm7(msg):
    global seen_msm_gps
    try:
        meta, sats, cells = parse_msm(msg)
    except Exception:
        return
    if not meta:
        return

    gnss = str(meta.get("gnss", "")).upper()
    if not gnss or gnss == "SBAS":
        return

    if msg.identity == "1077":
        seen_msm_gps = True

    # build GLONASS frequency channel map (PRN -> k)
    glo_k = {}
    if gnss == "GLONASS":
        for s in sats:
            prn = s.get("PRN")
            if prn is None:
                continue
            key = str(prn).zfill(3) if str(prn).isdigit() else str(prn)
            glo_k[key] = glonass_channel(s.get("DF419"))

    # collect per-PRN signal measurements
    by_prn: dict = {}
    for c in cells:
        prn = c.get("CELLPRN")
        sig = c.get("CELLSIG")
        if prn is None or sig is None:
            continue
        prn_key = str(prn).zfill(3) if str(prn).isdigit() else str(prn)
        band = str(sig).upper()
        d = by_prn.setdefault(prn_key, {})
        df406 = c.get("DF406")
        phase_m = float(df406) * 1e-3 * C_LIGHT if df406 is not None else None
        df405 = c.get("DF405")
        code_m = float(df405) * 1e-3 * C_LIGHT if df405 is not None else None
        d[band] = {
            "phase": phase_m,
            "code": code_m,
            "cn0": c.get("DF408"),
        }

    # create samples
    for prn_key, sigs in by_prn.items():
        prn_id = fmt_prn(gnss, prn_key)
        if not prn_id:
            continue
        if not is_allowed_prn(prn_id):
            continue

        cn0 = pick_cn0(gnss, sigs)
        s4 = np.nan
        if cn0 is not None:
            cn0_buf[prn_id].append(float(cn0))
            s4 = s4c(cn0_buf[prn_id]) if len(cn0_buf[prn_id]) == WIN_CNO else np.nan

        # Theory-based split:
        # - VTEC: code-derived STEC for absolute level
        # - ROTI: phase-derived STEC for high precision dynamics
        stec_phase = compute_stec(gnss, sigs, glo_k.get(prn_key), key="phase")
        stec_code = compute_stec(gnss, sigs, glo_k.get(prn_key), key="code")

        stec_for_vtec = stec_code
        stec_for_roti = stec_phase if stec_phase is not None else stec_code

        roti = np.nan
        vtec = None
        if stec_for_roti is not None:
            buf_s = stec_buf[prn_id]
            if buf_s and abs(stec_for_roti - buf_s[-1]) > SLIP_THR:
                buf_s.clear()
            buf_s.append(float(stec_for_roti))
            roti = np.sqrt(np.mean(np.diff(buf_s) ** 2)) if len(buf_s) == WIN_STEC else np.nan
        if stec_for_vtec is not None:
            try:
                t_sec = float(meta.get("epoch", 0)) / 1000.0
            except Exception:
                t_sec = None

            el = elev_deg_any(gnss, prn_id, t_sec) if t_sec is not None else None
            if el is not None and el < ELEV_CUT:
                continue
            if el is not None:
                m = slant_factor(el)
                vtec = float(stec_for_vtec / m)

        if vtec is None and not np.isfinite(s4) and not np.isfinite(roti):
            continue

        ts = now_utc().replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
        publish_q.put({
            "ts": ts,
            "prn": prn_id,
            "S4c": float(s4) if np.isfinite(s4) else None,
            "ROTI": float(roti) if np.isfinite(roti) else None,
            "VTEC": float(vtec) if vtec is not None and np.isfinite(vtec) else None,
        })


def handle_1004(msg):
    if seen_msm_gps:
        return
    # GPS only
    tow = msg.DF004 // 1000  # ms -> s (rough)
    nsat = msg.DF006

    for i in range(1, nsat + 1):
        prn_num = getattr(msg, f"DF009_{i:02d}")
        if prn_num is None:
            continue
        if int(prn_num) > 32:
            continue

        prn = f"G{int(prn_num):02d}"
        if not is_allowed_prn(prn):
            continue

        p1    = getattr(msg, f"DF011_{i:02d}")
        df017 = getattr(msg, f"DF017_{i:02d}")
        cn0   = getattr(msg, f"DF015_{i:02d}")

        if p1 is None or df017 is None or cn0 is None:
            continue
        if not (0 < p1 < 3e7):
            continue

        diff = abs(df017)
        p2 = p1 + diff

        cn0_buf[prn].append(float(cn0))
        s4 = s4c(cn0_buf[prn]) if len(cn0_buf[prn]) == WIN_CNO else np.nan

        stec = TEC_K * diff
        if stec > 500:
            continue

        buf_s = stec_buf[prn]
        if buf_s and abs(stec - buf_s[-1]) > SLIP_THR:
            buf_s.clear()
        buf_s.append(float(stec))

        roti = np.sqrt(np.mean(np.diff(buf_s) ** 2)) if len(buf_s) == WIN_STEC else np.nan

        el = elev_deg(prn, int(tow))
        if el is None:
            continue
        if el < ELEV_CUT:
            continue

        m = slant_factor(el)
        vtec = float(stec / m)

        ts = now_utc().replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
        publish_q.put({
            "ts": ts,
            "prn": prn,
            "S4c": float(s4) if np.isfinite(s4) else None,
            "ROTI": float(roti) if np.isfinite(roti) else None,
            "VTEC": float(vtec) if np.isfinite(vtec) else None,
        })


# -------------------- reader loop --------------------
def rtcm_reader(sock):
    seen = set()
    for _, msg in RTCMReader(sock, labelmsm=2):
        if msg.identity not in seen and len(seen) < 10:
            print("RTCM seen:", msg.identity)
            seen.add(msg.identity)

        if msg.identity in MSM7_IDS:
            handle_msm7(msg)
        elif msg.identity == "1019":
            handle_1019(msg)
        elif msg.identity == "1020":
            handle_1020(msg)
        elif msg.identity == "1042":
            handle_1042(msg)
        elif msg.identity == "1044":
            handle_1044(msg)
        elif msg.identity in ("1045", "1046"):
            handle_1045_1046(msg)
        elif msg.identity == "1004":
            handle_1004(msg)


def main():
    cli = client.NTRIPClient(CASTER, PORT, MOUNT, USER, PASSWORD)
    sock = cli.connect_to_ntrip()
    if sock is None:
        raise RuntimeError("NTRIP connect failed")

    print(f"Starting worker for station {STATION_ID} (mount {MOUNT}) @ {LAT},{LON},{ALT}")
    threading.Thread(target=publish_loop, daemon=True).start()
    threading.Thread(target=make_gga_sentence, args=(sock,), daemon=True).start()

    # block
    rtcm_reader(sock)


if __name__ == "__main__":
    main()
