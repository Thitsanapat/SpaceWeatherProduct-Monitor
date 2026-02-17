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
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict

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
WORKER_CONFIG_URL = "http://localhost:8000/api/worker_config"
PUBLISH_INTERVAL_SEC = 1.0
PUBLISH_MAX_BATCH = 500
PUBLISH_Q_MAX = 20000
RECONNECT_BASE_SEC = 2.0
RECONNECT_MAX_SEC = 60.0
WATCHDOG_SEC = 15.0
CONFIG_POLL_SEC = 5.0

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

WIN_CNO = 20
# TEC/ROTI parameters (1 Hz defaults)
SAMPLE_RATE_HZ = 1.0
ROTI_WIN_SEC = 300  # 5-min ROTI (more stable for 1 Hz)
ROTI_WIN = max(2, int(ROTI_WIN_SEC * SAMPLE_RATE_HZ))
ROTI_MIN = max(10, int(0.6 * ROTI_WIN))  # require enough samples
STEC_MEDIAN_N = 3  # small median filter before ROTI
SLIP_TECU_THR = 5.0  # cycle slip threshold (TECU jump at 1 Hz)
HATCH_N = 60  # phase-smoothed code window (samples)
CYCLE_GAP_SEC = 5
C_LIGHT = 299792458.0  # m/s
RANGE_MS = C_LIGHT * 0.001
RE_KM = 6371.0
IONO_H_KM = 350.0
STEC_MIN = 0.0
STEC_MAX = 250.0  # expected STEC/VTEC range (TECU)
VTEC_MIN = 0.0
VTEC_MAX = 150.0  # keep VTEC slightly tighter

# TEC const (GPS L1/L2)
F1, F2 = 1575.42e6, 1227.60e6
TEC_K = (F1**2 * F2**2) / (40.3 * (F1**2 - F2**2)) * 1e-16  # TECU / m

# MSM identities (multi-GNSS): accept MSM4/5/6/7
MSM_IDS = {
    "1074", "1075", "1076", "1077",
    "1084", "1085", "1086", "1087",
    "1094", "1095", "1096", "1097",
    "1114", "1115", "1116", "1117",
    "1124", "1125", "1126", "1127",
    "1134", "1135", "1136", "1137",
    "1107",  # SBAS (skip)
}
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


# -------------------- MSM7 observation model --------------------
@dataclass
class ObsSignal:
    sig_id: str
    code_m: Optional[float]
    phase_m: Optional[float]
    cn0: Optional[float]


@dataclass
class PrnObs:
    gnss: str
    prn: str
    t_sec: int
    k_glo: Optional[int] = None
    bands: Dict[str, ObsSignal] = field(default_factory=dict)


@dataclass
class Bucket:
    t_sec: int
    prns: Dict[str, PrnObs] = field(default_factory=dict)

# -------------------- RTCM logging (station inspection) --------------------
RTCM_REPORT_EVERY = 10.0
RTCM_STATS = defaultdict(lambda: {"n": 0, "t0": time.monotonic()})
RTCM_LAST_REPORT = time.monotonic()

OBS_MAP = {
    "1077": "GPS",
    "1075": "GPS",
    "1074": "GPS",
    "1004": "GPS",
    "1117": "QZS",
    "1115": "QZS",
    "1114": "QZS",
    "1097": "GAL",
    "1095": "GAL",
    "1094": "GAL",
    "1127": "BDS",
    "1125": "BDS",
    "1124": "BDS",
    "1087": "GLO",
    "1085": "GLO",
    "1084": "GLO",
    "1012": "GLO",
}
NAV_MAP = {
    "1019": "GPS",
    "1044": "QZS",
    "1046": "GAL",
    "1045": "GAL",
    "1042": "BDS",
    "1020": "GLO",
}

OBS_SEEN_EVER = defaultdict(set)
NAV_SEEN_EVER = defaultdict(set)

OBS_PRIORITY = {
    "GPS": ["1077", "1075", "1074", "1004"],
    "QZS": ["1117", "1115", "1114"],
    "GAL": ["1097", "1095", "1094"],
    "BDS": ["1127", "1125", "1124"],
    "GLO": ["1087", "1085", "1084", "1012"],
}
NAV_PRIORITY = {
    "GPS": ["1019"],
    "QZS": ["1044"],
    "GAL": ["1046", "1045"],
    "BDS": ["1042"],
    "GLO": ["1020"],
}

def _prio_str(system: str, prio_map: dict, seen_map: dict) -> str:
    prio = prio_map.get(system, [])
    seen = seen_map.get(system, set())
    avail = [m for m in prio if m in seen]
    return " > ".join(avail) if avail else "-"


def log_rtcm(identity: str):
    global RTCM_LAST_REPORT
    now = time.monotonic()
    st = RTCM_STATS[identity]
    st["n"] += 1

    if identity in OBS_MAP:
        OBS_SEEN_EVER[OBS_MAP[identity]].add(identity)
    if identity in NAV_MAP:
        NAV_SEEN_EVER[NAV_MAP[identity]].add(identity)

    if now - RTCM_LAST_REPORT >= RTCM_REPORT_EVERY:
        print(f"\n--- RTCM rates (last {RTCM_REPORT_EVERY:.0f}s) ---")
        for msg_id in sorted(RTCM_STATS.keys()):
            s = RTCM_STATS[msg_id]
            dt = now - s["t0"]
            hz = s["n"] / dt if dt > 0 else 0.0
            print(f"{msg_id}: {s['n']} ({hz:.2f} Hz)")
            s["n"] = 0
            s["t0"] = now

        print("--- OBS priority (seen, exclude SBAS/OTHER) ---")
        for sys in ("GPS", "QZS", "GAL", "BDS", "GLO"):
            print(f"{sys}: {_prio_str(sys, OBS_PRIORITY, OBS_SEEN_EVER)}")

        print("--- NAV priority (seen, exclude SBAS/OTHER) ---")
        for sys in ("GPS", "QZS", "GAL", "BDS", "GLO"):
            print(f"{sys}: {_prio_str(sys, NAV_PRIORITY, NAV_SEEN_EVER)}")

        RTCM_LAST_REPORT = now

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


def normalize_band(gnss: str, sig_id: str) -> Optional[str]:
    g = gnss.upper()
    s = str(sig_id).upper()

    # if already normalized, keep it
    KNOWN = {
        "L1", "L2", "L5",
        "E1", "E5A", "E5B", "E5AB", "E6",
        "B1", "B1C", "B2", "B2A", "B2B", "B3",
        "G1", "G2",
        "LEX", "L6",
        "S",
    }
    if s in KNOWN:
        return "LEX" if s == "L6" else s

    if g == "GPS":
        if "L1" in s:
            return "L1"
        if "L2" in s:
            return "L2"
        if "L5" in s:
            return "L5"
        return None

    if g == "GALILEO":
        if "E1" in s:
            return "E1"
        if "E5A" in s:
            return "E5A"
        if "E5B" in s:
            return "E5B"
        if "E5" in s:
            return "E5AB"
        if "E6" in s:
            return "E6"
        return None

    if g == "BEIDOU":
        if "B1C" in s:
            return "B1C"
        if "B1" in s:
            return "B1"
        if "B2A" in s:
            return "B2A"
        if "B2B" in s:
            return "B2B"
        if "B2" in s:
            return "B2"
        if "B3" in s:
            return "B3"
        return None

    if g == "QZSS":
        if "L1" in s:
            return "L1"
        if "L2" in s:
            return "L2"
        if "L5" in s:
            return "L5"
        if "LEX" in s or "L6" in s:
            return "LEX"
        return None

    if g == "GLONASS":
        if "L1" in s or "G1" in s:
            return "G1"
        if "L2" in s or "G2" in s:
            return "G2"
        return None

    if g in ("IRNSS", "NAVIC"):
        if "L5" in s:
            return "L5"
        if s.startswith("S") or "S" == s:
            return "S"
        return None

    return None


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
    avail = {
        b for b, d in sigs.items()
        if d.get(key) is not None and np.isfinite(d.get(key))
    }
    for b1, b2 in TEC_PAIR_PRIORITY.get(gnss.upper(), []):
        if b1 not in avail or b2 not in avail:
            continue
        p1 = sigs[b1].get(key)
        p2 = sigs[b2].get(key)
        if p1 is None or p2 is None:
            continue
        f1 = get_freq(gnss, b1, k_glonass)
        f2 = get_freq(gnss, b2, k_glonass)
        if not f1 or not f2 or f1 == f2:
            continue
        den = 40.3 * ((1.0 / (f2 ** 2)) - (1.0 / (f1 ** 2)))
        if den == 0:
            continue
        stec = float((p2 - p1) / den * 1e-16)
        return stec
    return None


def _clamp_range(v: Optional[float], vmin: float, vmax: float) -> Optional[float]:
    if v is None or not np.isfinite(v):
        return None
    x = float(v)
    if x < vmin:
        return vmin
    if x > vmax:
        return vmax
    return x


def slant_factor(el_deg: float):
    el = np.radians(el_deg)
    x = (RE_KM * np.cos(el) / (RE_KM + IONO_H_KM)) ** 2
    return 1.0 / np.sqrt(max(1.0 - x, 1e-12))


def _reset_prn_state(prn_id: str):
    rot_buf[prn_id].clear()
    prev_roti.pop(prn_id, None)
    smooth_state.pop(prn_id, None)
    phase_bias.pop(prn_id, None)
    phase_prev_corr.pop(prn_id, None)
    stec_buf.pop(prn_id, None)


def _merge_signal(existing: Optional[ObsSignal], new: ObsSignal) -> ObsSignal:
    if existing is None:
        return new
    # fill missing fields
    if existing.code_m is None and new.code_m is not None:
        existing.code_m = new.code_m
    if existing.phase_m is None and new.phase_m is not None:
        existing.phase_m = new.phase_m
    # prefer higher cn0 but never overwrite with None
    if new.cn0 is not None and (existing.cn0 is None or new.cn0 > existing.cn0):
        existing.cn0 = new.cn0
        existing.sig_id = new.sig_id
        if new.code_m is not None:
            existing.code_m = new.code_m
        if new.phase_m is not None:
            existing.phase_m = new.phase_m
    return existing


def _hatch_smooth(prn_id: str, band: str, code_m: Optional[float], phase_m: Optional[float], t_sec: int) -> Optional[float]:
    if code_m is None or phase_m is None:
        return code_m

    state = smooth_state[prn_id].get(band)
    if state is None:
        smooth_state[prn_id][band] = {"p_sm": code_m, "l_prev": phase_m, "t_prev": t_sec}
        return code_m

    dt = t_sec - state.get("t_prev", t_sec)
    if dt <= 0 or dt > CYCLE_GAP_SEC:
        # reset on large gap or time reversal
        smooth_state[prn_id][band] = {"p_sm": code_m, "l_prev": phase_m, "t_prev": t_sec}
        return code_m

    p_sm_prev = state["p_sm"]
    l_prev = state["l_prev"]
    n = max(2, HATCH_N)
    p_sm = (1.0 - 1.0 / n) * (p_sm_prev + (phase_m - l_prev)) + (1.0 / n) * code_m

    state["p_sm"] = p_sm
    state["l_prev"] = phase_m
    state["t_prev"] = t_sec
    return p_sm


def _phase_bias_correct(prn_id: str, stec_raw: Optional[float]) -> Optional[float]:
    if stec_raw is None or not np.isfinite(stec_raw):
        return None
    bias = phase_bias.get(prn_id, 0.0)
    corrected = float(stec_raw) + bias
    prev = phase_prev_corr.get(prn_id)
    if prev is not None and np.isfinite(prev):
        if abs(corrected - prev) > SLIP_TECU_THR:
            # adjust bias to keep continuity instead of hard reset
            bias = float(prev) - float(stec_raw)
            phase_bias[prn_id] = bias
            corrected = float(stec_raw) + bias
    phase_prev_corr[prn_id] = corrected
    return corrected


def _median_stec(prn_id: str, stec_val: float) -> float:
    buf = stec_buf[prn_id]
    buf.append(float(stec_val))
    if len(buf) == 1:
        return buf[0]
    return float(np.median(np.array(buf, dtype=float)))


def _msm_range_ms(val: Optional[float]) -> Optional[float]:
    # DF397/DF398/DF400/DF401/DF405/DF406 are in milliseconds (scaled)
    if val is None:
        return None
    try:
        x = float(val)
    except Exception:
        return None
    if not np.isfinite(x):
        return None
    return x


def _bands_dict(obs: PrnObs) -> dict:
    d = {}
    for band, s in obs.bands.items():
        d[band] = {"code": s.code_m, "phase": s.phase_m, "cn0": s.cn0}
    return d


def _bucket_ts_iso(t_sec: int) -> str:
    return datetime.fromtimestamp(t_sec, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


GPS_EPOCH = datetime(1980, 1, 6, tzinfo=timezone.utc)
WEEK_SEC = 604800.0

def _epoch_ms_to_unix_sec(epoch_ms: Optional[float]) -> Optional[int]:
    if epoch_ms is None:
        return None
    try:
        ems = float(epoch_ms)
    except Exception:
        return None
    if not np.isfinite(ems) or ems <= 0:
        return None
    # already unix ms
    if ems > 1.0e12:
        return int(ems / 1000.0)
    # likely unix seconds
    if ems > 1.0e9:
        return int(ems)
    # GNSS time-of-week (ms)
    now = datetime.now(timezone.utc)
    now_sec = (now - GPS_EPOCH).total_seconds()
    week = int(now_sec // WEEK_SEC)
    week_start = GPS_EPOCH + timedelta(seconds=week * WEEK_SEC)
    ts = week_start + timedelta(milliseconds=ems)
    return int(ts.timestamp())


def _flush_bucket(bucket: Bucket):
    for prn_id, obs in bucket.prns.items():
        sigs = _bands_dict(obs)
        if not sigs:
            continue

        cn0 = pick_cn0(obs.gnss, sigs)
        s4 = np.nan
        if cn0 is not None:
            cn0_buf[prn_id].append(float(cn0))
            s4 = s4c(cn0_buf[prn_id]) if len(cn0_buf[prn_id]) == WIN_CNO else np.nan

        # phase-based STEC (bias-corrected) for ROT/ROTI continuity
        stec_phase_raw = compute_stec(obs.gnss, sigs, obs.k_glo, key="phase")
        stec_phase = _phase_bias_correct(prn_id, stec_phase_raw)

        # phase-smoothed code for absolute STEC (Hatch filter)
        for band in list(sigs.keys()):
            code_m = sigs[band].get("code")
            phase_m = sigs[band].get("phase")
            sigs[band]["code"] = _hatch_smooth(prn_id, band, code_m, phase_m, bucket.t_sec)

        stec_code = compute_stec(obs.gnss, sigs, obs.k_glo, key="code")

        stec_for_vtec = _clamp_range(stec_code, STEC_MIN, STEC_MAX)
        stec_for_roti = stec_phase if stec_phase is not None else stec_code
        if stec_for_roti is not None:
            stec_for_roti = _median_stec(prn_id, stec_for_roti)
        # STEC plot: use code-level (Hatch-smoothed) to avoid phase ambiguity
        stec_out = _clamp_range(stec_code, STEC_MIN, STEC_MAX)
        if stec_out is None:
            stec_out = _clamp_range(stec_phase, STEC_MIN, STEC_MAX)

        roti = np.nan
        vtec = None
        if stec_for_roti is not None:
            # ROTI = std(diff(STEC)) over window (legacy-style, 1 Hz)
            prevr = prev_roti.get(prn_id)
            if prevr is not None:
                dt = float(bucket.t_sec - prevr)
                if dt <= 0 or dt > CYCLE_GAP_SEC:
                    _reset_prn_state(prn_id)
            prev_roti[prn_id] = bucket.t_sec

            rot_buf[prn_id].append(float(stec_for_roti))
            if len(rot_buf[prn_id]) >= ROTI_MIN:
                vals = np.array(rot_buf[prn_id], dtype=float)
                roti = float(np.nanstd(np.diff(vals)))

        if stec_for_vtec is not None:
            el = elev_deg_any(obs.gnss, prn_id, float(bucket.t_sec))
            if el is not None and el < ELEV_CUT:
                continue
            if el is not None:
                m = slant_factor(el)
                vtec = _clamp_range(float(stec_for_vtec / m), VTEC_MIN, VTEC_MAX)

        if stec_out is None and vtec is None and not np.isfinite(s4) and not np.isfinite(roti):
            continue

        ts = _bucket_ts_iso(bucket.t_sec)
        enqueue_publish({
            "ts": ts,
            "prn": prn_id,
            "S4c": float(s4) if np.isfinite(s4) else None,
            "ROTI": float(roti) if np.isfinite(roti) else None,
            "VTEC": float(vtec) if vtec is not None and np.isfinite(vtec) else None,
            "STEC": float(stec_out) if stec_out is not None and np.isfinite(stec_out) else None,
        })


def _get_bucket(t_sec: int) -> Optional[Bucket]:
    global current_bucket, current_bucket_sec
    if current_bucket is None:
        current_bucket = Bucket(t_sec)
        current_bucket_sec = t_sec
        return current_bucket

    if current_bucket_sec is None or t_sec == current_bucket_sec:
        return current_bucket

    if t_sec < current_bucket_sec:
        # out-of-order epoch; skip to avoid mixing buckets
        return None

    _flush_bucket(current_bucket)
    current_bucket = Bucket(t_sec)
    current_bucket_sec = t_sec
    return current_bucket


def enqueue_publish(item: dict):
    global publish_drop_count
    try:
        publish_q.put_nowait(item)
    except queue.Full:
        publish_drop_count += 1
        # avoid spamming logs; print occasionally
        if publish_drop_count % 1000 == 0:
            print(f"[publish] dropped {publish_drop_count} items (queue full)")


# -------------------- state --------------------
cn0_buf  = defaultdict(lambda: deque(maxlen=WIN_CNO))   # per-PRN
rot_buf = defaultdict(lambda: deque(maxlen=ROTI_WIN))   # per-PRN STEC window for ROTI
prev_roti = {}     # prn -> last t_sec
smooth_state = defaultdict(dict)  # prn -> band -> {p_sm, l_prev, t_prev}
phase_bias = {}    # prn -> bias (TECU)
phase_prev_corr = {}  # prn -> last corrected phase STEC
stec_buf = defaultdict(lambda: deque(maxlen=STEC_MEDIAN_N))  # per-PRN STEC median filter
eph_gps = {}
eph_gal = {}
eph_bds = {}
eph_qzs = {}
eph_glo = {}
seen_msm_gps = False
current_bucket: Optional[Bucket] = None
current_bucket_sec: Optional[int] = None

publish_q: "queue.Queue[dict]" = queue.Queue(maxsize=PUBLISH_Q_MAX)
publish_drop_count = 0
last_rtcm_time = time.monotonic()


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
    buf: Dict[tuple, dict] = {}
    last_flush = time.monotonic()
    session = requests.Session()

    while True:
        try:
            item = publish_q.get(timeout=0.2)
            key = (item.get("ts"), item.get("prn"))
            buf[key] = item
        except queue.Empty:
            pass

        now = time.monotonic()
        if buf and (len(buf) >= PUBLISH_MAX_BATCH or (now - last_flush) >= PUBLISH_INTERVAL_SEC):
            payload = {"station": STATION_ID, "data": list(buf.values())}
            try:
                session.post(BACKEND_PUBLISH, json=payload, timeout=2.0)
            except Exception:
                # network/backend down -> keep worker alive
                pass
            buf.clear()
            last_flush = now


# -------------------- NTRIP --------------------
def make_gga_sentence(sock, stop_event: Optional[threading.Event] = None):
    while True:
        if stop_event is not None and stop_event.is_set():
            break
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


def watchdog_loop(sock, stop_event: threading.Event):
    global last_rtcm_time
    while not stop_event.is_set():
        if time.monotonic() - last_rtcm_time > WATCHDOG_SEC:
            print(f"[watchdog] no RTCM for {WATCHDOG_SEC:.0f}s -> reconnect")
            try:
                sock.shutdown(2)
            except Exception:
                pass
            try:
                sock.close()
            except Exception:
                pass
            stop_event.set()
            return
        time.sleep(1.0)


def poll_config_loop(stop_event: Optional[threading.Event] = None):
    global ELEV_CUT
    session = requests.Session()
    while True:
        if stop_event is not None and stop_event.is_set():
            return
        try:
            resp = session.get(WORKER_CONFIG_URL, params={"station": STATION_ID}, timeout=2.0)
            if resp.ok:
                cfg = resp.json().get("config", {})
                elev = cfg.get("elev_cut")
                if elev is not None:
                    try:
                        elev = float(elev)
                        elev = max(0.0, min(90.0, elev))
                        if abs(elev - ELEV_CUT) > 1e-6:
                            print(f"[config] ELEV_CUT updated: {ELEV_CUT:.1f} -> {elev:.1f}")
                            ELEV_CUT = elev
                    except Exception:
                        pass
        except Exception:
            pass
        time.sleep(CONFIG_POLL_SEC)


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

    try:
        epoch_ms = meta.get("epoch", 0)
    except Exception:
        epoch_ms = 0
    t_sec = _epoch_ms_to_unix_sec(epoch_ms)
    if t_sec is None:
        t_sec = int(time.time())

    bucket = _get_bucket(t_sec)
    if bucket is None:
        return

    # build GLONASS frequency channel map (PRN -> k) and rough range (ms)
    glo_k = {}
    rough_ms_by_prn = {}
    if gnss == "GLONASS":
        for s in sats:
            prn = s.get("PRN")
            if prn is None:
                continue
            key = str(prn).zfill(3) if str(prn).isdigit() else str(prn)
            glo_k[key] = glonass_channel(s.get("DF419"))
            df397 = _msm_range_ms(s.get("DF397"))
            df398 = _msm_range_ms(s.get("DF398"))
            if df397 is not None and df398 is not None:
                rough_ms_by_prn[key] = df397 + df398
    else:
        for s in sats:
            prn = s.get("PRN")
            if prn is None:
                continue
            key = str(prn).zfill(3) if str(prn).isdigit() else str(prn)
            df397 = _msm_range_ms(s.get("DF397"))
            df398 = _msm_range_ms(s.get("DF398"))
            if df397 is not None and df398 is not None:
                rough_ms_by_prn[key] = df397 + df398

    # collect per-PRN signal measurements into current bucket
    for c in cells:
        prn = c.get("CELLPRN")
        sig = c.get("CELLSIG")
        if prn is None or sig is None:
            continue

        prn_key = str(prn).zfill(3) if str(prn).isdigit() else str(prn)
        prn_id = fmt_prn(gnss, prn_key)
        if not prn_id:
            continue
        if not is_allowed_prn(prn_id):
            continue

        band = normalize_band(gnss, str(sig))
        if band is None:
            continue

        rough_ms = rough_ms_by_prn.get(prn_key)
        # MSM4/5 use DF400/DF401; MSM6/7 use DF405/DF406
        fine_code = _msm_range_ms(c.get("DF405"))
        if fine_code is None:
            fine_code = _msm_range_ms(c.get("DF400"))
        fine_phase = _msm_range_ms(c.get("DF406"))
        if fine_phase is None:
            fine_phase = _msm_range_ms(c.get("DF401"))

        code_m = (rough_ms + fine_code) * RANGE_MS if rough_ms is not None and fine_code is not None else None
        phase_m = (rough_ms + fine_phase) * RANGE_MS if rough_ms is not None and fine_phase is not None else None
        cn0 = c.get("DF408")

        prn_obs = bucket.prns.get(prn_id)
        if prn_obs is None:
            prn_obs = PrnObs(gnss, prn_id, bucket.t_sec, k_glo=glo_k.get(prn_key))
            bucket.prns[prn_id] = prn_obs

        new_sig = ObsSignal(str(sig), code_m, phase_m, cn0)
        prn_obs.bands[band] = _merge_signal(prn_obs.bands.get(band), new_sig)


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
        stec = _clamp_range(stec, STEC_MIN, STEC_MAX)
        if stec is None:
            continue
        if stec > 500:
            continue

        # ROTI (fallback, code-based): std(diff(STEC)) over window
        t_sec = int(tow) if tow is not None else int(time.time())
        prevr = prev_roti.get(prn)
        if prevr is not None:
            dt = float(t_sec - prevr)
            if dt <= 0 or dt > CYCLE_GAP_SEC:
                _reset_prn_state(prn)
        prev_roti[prn] = t_sec
        rot_buf[prn].append(float(stec))
        roti = float(np.nanstd(np.diff(np.array(rot_buf[prn], dtype=float)))) if len(rot_buf[prn]) >= ROTI_MIN else np.nan

        el = elev_deg(prn, int(tow))
        if el is None:
            continue
        if el < ELEV_CUT:
            continue

        m = slant_factor(el)
        vtec = float(stec / m)

        ts = now_utc().replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
        enqueue_publish({
            "ts": ts,
            "prn": prn,
            "S4c": float(s4) if np.isfinite(s4) else None,
            "ROTI": float(roti) if np.isfinite(roti) else None,
            "VTEC": float(vtec) if np.isfinite(vtec) else None,
            "STEC": float(stec) if np.isfinite(stec) else None,
        })


# -------------------- reader loop --------------------
def rtcm_reader(sock, stop_event: Optional[threading.Event] = None):
    global last_rtcm_time
    for _, msg in RTCMReader(sock, labelmsm=2):
        if stop_event is not None and stop_event.is_set():
            break
        last_rtcm_time = time.monotonic()
        log_rtcm(msg.identity)

        if msg.identity in MSM_IDS:
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
    print(f"Starting worker for station {STATION_ID} (mount {MOUNT}) @ {LAT},{LON},{ALT}")
    threading.Thread(target=publish_loop, daemon=True).start()
    threading.Thread(target=poll_config_loop, daemon=True).start()

    backoff = RECONNECT_BASE_SEC
    while True:
        sock = None
        stop_event = threading.Event()
        try:
            cli = client.NTRIPClient(CASTER, PORT, MOUNT, USER, PASSWORD)
            sock = cli.connect_to_ntrip()
            if sock is None:
                raise RuntimeError("NTRIP connect failed")

            # reset watchdog timer on new connection
            global last_rtcm_time
            last_rtcm_time = time.monotonic()

            threading.Thread(target=make_gga_sentence, args=(sock, stop_event), daemon=True).start()
            threading.Thread(target=watchdog_loop, args=(sock, stop_event), daemon=True).start()

            # block until socket closes or watchdog triggers
            rtcm_reader(sock, stop_event=stop_event)
        except Exception as e:
            print(f"[worker] error: {e}")
        finally:
            stop_event.set()
            try:
                if sock is not None:
                    sock.close()
            except Exception:
                pass
            try:
                if current_bucket is not None:
                    _flush_bucket(current_bucket)
            except Exception:
                pass

        # reconnect with backoff
        time.sleep(backoff)
        backoff = min(backoff * 2.0, RECONNECT_MAX_SEC)


if __name__ == "__main__":
    main()
