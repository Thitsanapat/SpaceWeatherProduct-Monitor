# backend/main.py
# -*- coding: utf-8 -*-

import asyncio
import csv
import io
import json
import math
import os
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from threading import Lock
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd
from PIL import Image
from roboflow import Roboflow
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Body
from fastapi.middleware.cors import CORSMiddleware
from stations import DEFAULT_STATION_ID, get_station, list_station_ids, list_stations, normalize_station_id

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"   # backend/data/<station>/<date>.csv

CSV_HEADER = ["ts", "prn", "S4c", "ROTI", "VTEC", "STEC", "station"]
PUBLISH_MAX_ROWS = 5000
STEC_MIN = 0.0
STEC_MAX = 250.0
VTEC_MIN = 0.0
VTEC_MAX = 250.0
WS_HEARTBEAT_SEC = 5.0
WS_CLIENT_IDLE_SEC = 20.0
PHASE2_AI_CACHE_TTL_SEC = 20.0

RUNTIME_METRICS = {
    "publish": {
        "last_station": None,
        "last_rows": 0,
        "last_write_ms": 0.0,
        "last_broadcast_ms": 0.0,
        "last_total_ms": 0.0,
        "last_at": None,
        "count": 0,
    }
}
CLIENT_METRICS: Dict[str, Dict] = {}
STATION_LATEST: Dict[str, Dict] = {}
PHASE2_AI_CACHE: Dict[str, Dict] = {}
ROBOFLOW_MODEL_CACHE: Dict[str, object] = {}
ROBOFLOW_EXECUTOR = ThreadPoolExecutor(max_workers=2)
ROBOFLOW_INFER_LOCK = Lock()

# ----------------------------
# app
# ----------------------------
app = FastAPI(title="GNSS Monitor Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://localhost:3001"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Test if WebSocket decorator works at all
@app.websocket("/ws/test")
async def ws_test(ws: WebSocket):
    await ws.accept()
    await ws.send_text("test")

# in-memory worker config (per station)
WORKER_CONFIG: Dict[str, Dict] = {}

# ----------------------------
# websocket manager
# ----------------------------
class ConnectionManager:
    def __init__(self):
        self._conns: Dict[str, set] = {}
        self._lock = asyncio.Lock()

    async def register(self, station: str, ws: WebSocket):
        async with self._lock:
            self._conns.setdefault(station, set()).add(ws)

    async def unregister(self, station: str, ws: WebSocket):
        async with self._lock:
            s = self._conns.get(station)
            if s and ws in s:
                s.remove(ws)

    async def broadcast(self, station: str, message: str):
        async with self._lock:
            targets = list(self._conns.get(station, set()))
        dead = []
        for ws in targets:
            try:
                await ws.send_text(message)
            except Exception:
                dead.append(ws)
        if dead:
            async with self._lock:
                for ws in dead:
                    self._conns.get(station, set()).discard(ws)

    async def count_station(self, station: str) -> int:
        async with self._lock:
            return len(self._conns.get(station, set()))

    async def count_total(self) -> int:
        async with self._lock:
            return sum(len(v) for v in self._conns.values())

manager = ConnectionManager()

# ----------------------------
# helpers
# ----------------------------
def utc_today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")

def date_from_ts(ts: str) -> str:
    try:
        dt = pd.to_datetime(ts, errors="coerce", utc=True)
        if pd.isna(dt):
            return utc_today_str()
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return utc_today_str()

def ensure_daily_csv(station: str, date_yyyy_mm_dd: str) -> Path:
    station_dir = DATA_DIR / station
    station_dir.mkdir(parents=True, exist_ok=True)
    p = station_dir / f"{date_yyyy_mm_dd}.csv"
    if not p.exists():
        with p.open("w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(CSV_HEADER)
    return p

def append_rows_to_csv(csv_path: Path, rows: List[Dict]):
    if not rows:
        return
    with csv_path.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        for r in rows:
            w.writerow([
                r.get("ts", ""),
                r.get("prn", ""),
                r.get("S4c", ""),
                r.get("ROTI", ""),
                r.get("VTEC", ""),
                r.get("STEC", ""),
                r.get("station", ""),
            ])

def is_allowed_prn(prn: str) -> bool:
    if not prn:
        return False
    s = prn[0].upper()
    # ✅ ไม่เอา SBAS(S) และไม่เอา OTHER/unknown
    return s in ("G", "E", "C", "R", "J", "I")

def _filter_range(v, vmin: float, vmax: float):
    try:
        if v is None:
            return None
        x = float(v)
        if not (vmin <= x <= vmax):
            return None
        return x
    except Exception:
        return None

def _float_or_none(v):
    try:
        if v is None or v == "":
            return None
        x = float(v)
        return x if pd.notna(x) else None
    except Exception:
        return None


def _clamp_lat(lat: float) -> float:
    return max(-89.9, min(89.9, float(lat)))


def _wrap_lon(lon: float) -> float:
    x = float(lon)
    return ((x + 180.0) % 360.0) - 180.0


def _stable_hash_u32(s: str) -> int:
    h = 2166136261
    for ch in s:
        h ^= ord(ch)
        h = (h * 16777619) & 0xFFFFFFFF
    return h


def _proxy_ipp(st_lat: float, st_lon: float, prn: str, ts: str, roti: float) -> Dict[str, float]:
    # Proxy IPP placement around station center when full ionospheric pierce-point geometry is unavailable.
    seed = _stable_hash_u32(f"{prn}|{ts}")
    angle_deg = float(seed % 360)
    angle = math.radians(angle_deg)
    ring_deg = 1.2 + min(4.5, max(0.0, roti) * 3.5)

    dlat = ring_deg * math.sin(angle)
    cos_lat = max(0.2, math.cos(math.radians(st_lat)))
    dlon = (ring_deg * math.cos(angle)) / cos_lat
    lat = _clamp_lat(st_lat + dlat)
    lon = _wrap_lon(st_lon + dlon)
    return {"lat": lat, "lon": lon}


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2.0) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2.0) ** 2
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(max(0.0, 1.0 - a)))
    return r * c


def _point_in_polygon(lat: float, lon: float, polygon: List[Dict]) -> bool:
    if not polygon or len(polygon) < 3:
        return False
    inside = False
    x = float(lon)
    y = float(lat)
    n = len(polygon)
    j = n - 1
    for i in range(n):
        xi = float(polygon[i].get("lon", 0.0))
        yi = float(polygon[i].get("lat", 0.0))
        xj = float(polygon[j].get("lon", 0.0))
        yj = float(polygon[j].get("lat", 0.0))
        crosses = (yi > y) != (yj > y)
        if crosses:
            x_inter = (xj - xi) * (y - yi) / ((yj - yi) or 1e-12) + xi
            if x < x_inter:
                inside = not inside
        j = i
    return inside


def _severity_from_roti(roti: float) -> str:
    if roti >= 0.8:
        return "high"
    if roti >= 0.55:
        return "medium"
    return "low"


def _segment_overlays(events: List[Dict]) -> List[Dict]:
    if not events:
        return []

    # Cluster by severity so frontend can render distinct alert regions.
    buckets: Dict[str, List[Dict]] = {"high": [], "medium": [], "low": []}
    for e in events:
        sev = str(e.get("severity", "low"))
        buckets.setdefault(sev, []).append(e)

    segments: List[Dict] = []
    for sev in ("high", "medium", "low"):
        pts = buckets.get(sev, [])
        if not pts:
            continue

        lat_mean = sum(float(p["lat"]) for p in pts) / len(pts)
        lon_mean = sum(float(p["lon"]) for p in pts) / len(pts)
        max_roti = max(float(p.get("roti") or 0.0) for p in pts)
        span = 0.8 + min(2.8, max_roti * 2.0)

        poly = [
            {"lat": _clamp_lat(lat_mean + span), "lon": _wrap_lon(lon_mean)},
            {"lat": _clamp_lat(lat_mean + span * 0.35), "lon": _wrap_lon(lon_mean + span * 1.1)},
            {"lat": _clamp_lat(lat_mean - span * 0.85), "lon": _wrap_lon(lon_mean + span * 0.55)},
            {"lat": _clamp_lat(lat_mean - span * 0.45), "lon": _wrap_lon(lon_mean - span * 1.05)},
        ]
        segments.append(
            {
                "id": f"seg-{sev}",
                "label": f"{sev.upper()} ROTI zone",
                "severity": sev,
                "confidence": round(0.68 + min(0.29, max_roti / 2.0), 3),
                "source": "heuristic-phase2",
                "polygon": poly,
            }
        )

    return segments


def _world_to_pixel(lat: float, lon: float, center_lat: float, center_lon: float, span_lat: float, span_lon: float, w: int, h: int):
    x = (float(lon) - (center_lon - span_lon)) / (2.0 * span_lon)
    y = ((center_lat + span_lat) - float(lat)) / (2.0 * span_lat)
    px = int(max(0, min(w - 1, round(x * (w - 1)))))
    py = int(max(0, min(h - 1, round(y * (h - 1)))))
    return px, py


def _pixel_to_world(px: float, py: float, center_lat: float, center_lon: float, span_lat: float, span_lon: float, w: int, h: int):
    lon = (float(px) / max(1.0, float(w - 1))) * (2.0 * span_lon) + (center_lon - span_lon)
    lat = (center_lat + span_lat) - (float(py) / max(1.0, float(h - 1))) * (2.0 * span_lat)
    return _clamp_lat(lat), _wrap_lon(lon)


def _draw_dot(img: bytearray, w: int, h: int, x: int, y: int, r: int, rgb: tuple):
    rr = max(1, int(r))
    r2 = rr * rr
    for dy in range(-rr, rr + 1):
        yy = y + dy
        if yy < 0 or yy >= h:
            continue
        for dx in range(-rr, rr + 1):
            xx = x + dx
            if xx < 0 or xx >= w:
                continue
            if (dx * dx + dy * dy) > r2:
                continue
            idx = (yy * w + xx) * 3
            img[idx] = int(rgb[0])
            img[idx + 1] = int(rgb[1])
            img[idx + 2] = int(rgb[2])


def _build_white_canvas_png(events: List[Dict], center_lat: float, center_lon: float, span_lat: float, span_lon: float, w: int = 640, h: int = 640):
    # White background matches the Roboflow training domain and reduces visual noise.
    img = bytearray([255] * (w * h * 3))
    max_roti = max([float(e.get("roti") or 0.0) for e in events], default=1.0)

    for e in events:
        lat = float(e["lat"])
        lon = float(e["lon"])
        roti = float(e.get("roti") or 0.0)
        px, py = _world_to_pixel(lat, lon, center_lat, center_lon, span_lat, span_lon, w, h)
        if e.get("severity") == "high":
            color = (225, 29, 72)
        elif e.get("severity") == "medium":
            color = (245, 158, 11)
        else:
            color = (14, 165, 233)
        radius = 3 + int(min(14, max(0.0, roti) / max(0.05, max_roti) * 10))
        _draw_dot(img, w, h, px, py, radius, color)

    image = Image.frombytes("RGB", (w, h), bytes(img))
    bio = io.BytesIO()
    image.save(bio, format="PNG")
    return bio.getvalue(), {"w": w, "h": h, "center_lat": center_lat, "center_lon": center_lon, "span_lat": span_lat, "span_lon": span_lon}


def _get_roboflow_model(api_key: str, workspace: str, project_slug: str, version: int):
    cache_key = f"{workspace}/{project_slug}/{version}"
    m = ROBOFLOW_MODEL_CACHE.get(cache_key)
    if m is not None:
        return m
    rf = Roboflow(api_key=api_key)
    project = rf.workspace(workspace).project(project_slug)
    model = project.version(version).model
    ROBOFLOW_MODEL_CACHE[cache_key] = model
    return model


def _bbox_to_polygon(pred: Dict, mapping: Dict):
    x = float(pred.get("x") or 0.0)
    y = float(pred.get("y") or 0.0)
    bw = max(1.0, float(pred.get("width") or 1.0))
    bh = max(1.0, float(pred.get("height") or 1.0))
    corners = [
        (x - bw / 2.0, y - bh / 2.0),
        (x + bw / 2.0, y - bh / 2.0),
        (x + bw / 2.0, y + bh / 2.0),
        (x - bw / 2.0, y + bh / 2.0),
    ]
    poly = []
    for px, py in corners:
        lat, lon = _pixel_to_world(px, py, mapping["center_lat"], mapping["center_lon"], mapping["span_lat"], mapping["span_lon"], mapping["w"], mapping["h"])
        poly.append({"lat": lat, "lon": lon})
    return poly


def _run_roboflow_segmentation(events: List[Dict], station: str, date: str, roti_thr: float, user_lat: Optional[float], user_lon: Optional[float], user_radius_km: float, max_segments: int = 6):
    api_key = (os.getenv("ROBOFLOW_API_KEY") or "FVSp8sZuWwmacj8J4sim").strip()
    workspace = (os.getenv("ROBOFLOW_WORKSPACE") or "gnss-osejd").strip()
    project_slug = (os.getenv("ROBOFLOW_PROJECT") or "gnss-pn4dm").strip()
    version = int(os.getenv("ROBOFLOW_VERSION") or "9")
    if not api_key:
        return {"ok": False, "reason": "missing ROBOFLOW_API_KEY", "segments": []}

    key = json.dumps(
        {
            "station": station,
            "date": date,
            "roti_thr": round(float(roti_thr), 3),
            "user_lat": None if user_lat is None else round(float(user_lat), 3),
            "user_lon": None if user_lon is None else round(float(user_lon), 3),
            "radius": round(float(user_radius_km), 1),
            "events": [e.get("id") for e in events[:80]],
        },
        sort_keys=True,
    )
    now = time.time()
    cached = PHASE2_AI_CACHE.get(key)
    if cached and (now - float(cached.get("at", 0))) <= PHASE2_AI_CACHE_TTL_SEC:
        return {"ok": True, "cached": True, "segments": cached.get("segments", [])}

    if user_lat is not None and user_lon is not None:
        center_lat = _clamp_lat(float(user_lat))
        center_lon = _wrap_lon(float(user_lon))
        span_lat = max(1.2, min(9.0, float(user_radius_km) / 111.0 * 1.25))
    else:
        lat_mean = sum(float(e["lat"]) for e in events) / max(1, len(events))
        lon_mean = sum(float(e["lon"]) for e in events) / max(1, len(events))
        center_lat = _clamp_lat(lat_mean)
        center_lon = _wrap_lon(lon_mean)
        span_lat = 5.0

    cos_lat = max(0.2, math.cos(math.radians(center_lat)))
    span_lon = span_lat / cos_lat

    png_bytes, mapping = _build_white_canvas_png(events, center_lat, center_lon, span_lat, span_lon)

    def _predict_task():
        model = _get_roboflow_model(api_key, workspace, project_slug, version)
        with tempfile.NamedTemporaryFile(prefix="phase2_", suffix=".png", delete=False) as tf:
            tf.write(png_bytes)
            temp_path = tf.name
        try:
            try:
                return model.predict(temp_path, confidence=25).json()
            except TypeError:
                return model.predict(temp_path).json()
        finally:
            try:
                os.remove(temp_path)
            except Exception:
                pass

    if not ROBOFLOW_INFER_LOCK.acquire(blocking=False):
        return {"ok": False, "reason": "roboflow busy", "segments": []}
    try:
        future = ROBOFLOW_EXECUTOR.submit(_predict_task)
        payload = future.result(timeout=8.0)
    except FuturesTimeoutError:
        return {"ok": False, "reason": "roboflow sdk timeout", "segments": []}
    except Exception as e:
        return {"ok": False, "reason": f"roboflow sdk failed: {e}", "segments": []}
    finally:
        ROBOFLOW_INFER_LOCK.release()

    preds = payload.get("predictions") or []
    segments = []
    for i, pred in enumerate(preds[:max_segments]):
        conf = float(pred.get("confidence") or 0.0)
        sev = "low"
        if conf >= 0.75:
            sev = "high"
        elif conf >= 0.5:
            sev = "medium"

        pts = pred.get("points") or []
        poly = []
        if isinstance(pts, list) and len(pts) >= 3:
            for pt in pts:
                px = float(pt.get("x") or 0.0)
                py = float(pt.get("y") or 0.0)
                lat, lon = _pixel_to_world(px, py, mapping["center_lat"], mapping["center_lon"], mapping["span_lat"], mapping["span_lon"], mapping["w"], mapping["h"])
                poly.append({"lat": lat, "lon": lon})
        if len(poly) < 3:
            poly = _bbox_to_polygon(pred, mapping)

        segments.append(
            {
                "id": f"ai-seg-{i}",
                "label": str(pred.get("class") or "AI zone"),
                "severity": sev,
                "confidence": round(conf, 4),
                "source": "roboflow-white-canvas",
                "polygon": poly,
            }
        )

    if not segments:
        return {"ok": True, "cached": False, "segments": [], "reason": "roboflow returned 0 predictions"}

    PHASE2_AI_CACHE[key] = {"at": now, "segments": segments}
    return {"ok": True, "cached": False, "segments": segments}

def load_station_day_csv(station: str, date_yyyy_mm_dd: str) -> Optional[pd.DataFrame]:
    p = DATA_DIR / station / f"{date_yyyy_mm_dd}.csv"
    if not p.exists():
        return None
    try:
        df = pd.read_csv(p, dtype=str)
    except Exception:
        return None
    if df.empty:
        return None

    # normalize + filter
    if "ts" not in df.columns or "prn" not in df.columns:
        return None

    df["ts"] = pd.to_datetime(df["ts"], errors="coerce", utc=True)
    df = df.dropna(subset=["ts", "prn"])
    df["prn"] = df["prn"].astype(str)
    df = df[df["prn"].map(is_allowed_prn)]

    for col in ("S4c", "ROTI", "VTEC", "STEC"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        else:
            df[col] = pd.NA

    if "STEC" in df.columns:
        df.loc[(df["STEC"] < STEC_MIN) | (df["STEC"] > STEC_MAX), "STEC"] = pd.NA
    if "VTEC" in df.columns:
        df.loc[(df["VTEC"] < VTEC_MIN) | (df["VTEC"] > VTEC_MAX), "VTEC"] = pd.NA

    df = df.dropna(subset=["S4c", "ROTI", "VTEC", "STEC"], how="all")
    if df.empty:
        return None

    df["ts"] = df["ts"].dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    df = df.sort_values("ts").reset_index(drop=True)
    return df


def load_station_day_roti_csv(station: str, date_yyyy_mm_dd: str) -> Optional[pd.DataFrame]:
    p = DATA_DIR / station / f"{date_yyyy_mm_dd}.csv"
    if not p.exists():
        return None
    try:
        with p.open("r", encoding="utf-8", errors="ignore") as f:
            header = f.readline().strip()
        if not header:
            return None

        # Read only the last chunk of lines to keep all-stations queries responsive.
        n = 2500
        with p.open("rb") as f:
            f.seek(0, 2)
            file_size = f.tell()
            block = 8192
            data = b""
            while file_size > 0 and data.count(b"\n") < (n + 20):
                read_size = min(block, file_size)
                file_size -= read_size
                f.seek(file_size)
                data = f.read(read_size) + data

        tail_lines = data.decode("utf-8", errors="ignore").splitlines()
        tail_lines = [ln for ln in tail_lines if ln and not ln.startswith("ts,")]
        if not tail_lines:
            return None
        sample = tail_lines[-n:]

        rows = []
        reader = csv.DictReader([header] + sample)
        for r in reader:
            ts = r.get("ts")
            prn = r.get("prn")
            roti = r.get("ROTI")
            if ts is None or prn is None or roti is None:
                continue
            rows.append({"ts": ts, "prn": prn, "ROTI": roti})

        if not rows:
            return None
        df = pd.DataFrame(rows)
    except Exception:
        return None
    if df.empty:
        return None

    df["ts"] = pd.to_datetime(df["ts"], errors="coerce", utc=True)
    df = df.dropna(subset=["ts", "prn"])
    if df.empty:
        return None

    df["prn"] = df["prn"].astype(str)
    df = df[df["prn"].map(is_allowed_prn)]
    if df.empty:
        return None

    df["ROTI"] = pd.to_numeric(df["ROTI"], errors="coerce")
    df = df.dropna(subset=["ROTI"])
    if df.empty:
        return None

    df["ts"] = df["ts"].dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    return df.sort_values("ts").reset_index(drop=True)

# ----------------------------
# REST
# ----------------------------
@app.get("/api/stations")
def get_stations():
    return {"stations": list_stations()}

@app.get("/api/history")
def get_history(station: str, date: str, limit: int = 200000):
    station = normalize_station_id(station)
    if not station:
        return {"ok": False, "reason": "missing station", "rows": []}

    df = load_station_day_csv(station, date)
    if df is None:
        return {"ok": False, "reason": "csv not found or wrong columns", "rows": []}

    if len(df) > limit:
        df = df.iloc[-limit:].copy()

    rows = df[["ts", "prn", "S4c", "ROTI", "VTEC", "STEC"]].to_dict(orient="records")
    return {"ok": True, "rows": rows}


@app.get("/api/worker_config")
def get_worker_config(station: str):
    station = normalize_station_id(station) or DEFAULT_STATION_ID
    cfg = WORKER_CONFIG.get(station, {})
    return {"ok": True, "station": station, "config": cfg}


@app.post("/api/worker_config")
def set_worker_config(payload: Dict = Body(...)):
    station = normalize_station_id(payload.get("station")) or DEFAULT_STATION_ID
    cfg = WORKER_CONFIG.setdefault(station, {})
    if "elev_cut" in payload:
        try:
            v = float(payload.get("elev_cut"))
            v = max(0.0, min(90.0, v))
            cfg["elev_cut"] = v
        except Exception:
            pass
    return {"ok": True, "station": station, "config": cfg}

# ----------------------------
# WebSocket realtime
# ----------------------------
@app.websocket("/ws/realtime")
async def ws_realtime(ws: WebSocket):
    await ws.accept()
    station = DEFAULT_STATION_ID
    # allow station to be provided as query param (e.g. ws://.../ws/realtime?station=KMIT6)
    try:
        qs_station = ws.query_params.get("station")
        if qs_station:
            station = qs_station

        # try to read a short initial payload (clients may send JSON {station:...})
        try:
            init = await asyncio.wait_for(ws.receive_text(), timeout=1.0)
            try:
                payload = json.loads(init)
                station = payload.get("station", station)
            except Exception:
                pass
        except asyncio.TimeoutError:
            # no initial message, continue with query param or default
            pass

        station = normalize_station_id(station) or DEFAULT_STATION_ID
        await manager.register(station, ws)
        station_conn = await manager.count_station(station)
        total_conn = await manager.count_total()
        print(f"[WS] connected station={station} station_conn={station_conn} total_conn={total_conn}")
        # acknowledge registration so client knows it's ready
        await ws.send_text(json.dumps({"ok": True, "station": station, "msg": "registered"}))

        # keep alive + heartbeat + client liveness check
        last_client_msg = time.monotonic()
        while True:
            try:
                text = await asyncio.wait_for(ws.receive_text(), timeout=WS_HEARTBEAT_SEC)
                last_client_msg = time.monotonic()
                try:
                    incoming = json.loads(text)
                    if isinstance(incoming, dict) and incoming.get("type") == "ping":
                        await ws.send_text(json.dumps({"type": "pong", "ts": datetime.now(timezone.utc).isoformat()}))
                except Exception:
                    pass
            except asyncio.TimeoutError:
                await ws.send_text(json.dumps({"type": "heartbeat", "ts": datetime.now(timezone.utc).isoformat()}))
                if (time.monotonic() - last_client_msg) > WS_CLIENT_IDLE_SEC:
                    print(f"[WS] idle timeout station={station}")
                    await ws.close()
                    break

    except WebSocketDisconnect:
        await manager.unregister(station, ws)
        station_conn = await manager.count_station(station)
        total_conn = await manager.count_total()
        print(f"[WS] disconnected station={station} station_conn={station_conn} total_conn={total_conn}")
        return
    except Exception:
        await manager.unregister(station, ws)
        station_conn = await manager.count_station(station)
        total_conn = await manager.count_total()
        print(f"[WS] error-disconnect station={station} station_conn={station_conn} total_conn={total_conn}")
        return


@app.get("/api/runtime_metrics")
async def get_runtime_metrics(station: Optional[str] = None):
    station_norm = normalize_station_id(station) if station else None
    station_conn = await manager.count_station(station_norm) if station_norm else None
    total_conn = await manager.count_total()
    return {
        "ok": True,
        "ws": {
            "total_connections": total_conn,
            "station": station_norm,
            "station_connections": station_conn,
        },
        "publish": RUNTIME_METRICS.get("publish", {}),
        "client": CLIENT_METRICS.get(station_norm, {}) if station_norm else CLIENT_METRICS,
    }


@app.post("/api/client_metrics")
def set_client_metrics(payload: Dict = Body(...)):
    station = normalize_station_id(payload.get("station")) or DEFAULT_STATION_ID
    CLIENT_METRICS[station] = {
        "station": station,
        "render_ms": payload.get("render_ms"),
        "heap_mb": payload.get("heap_mb"),
        "ws_reconnects": payload.get("ws_reconnects"),
        "pending_samples": payload.get("pending_samples"),
        "pending_counts": payload.get("pending_counts"),
        "fps": payload.get("fps"),
        "ws_status": payload.get("ws_status"),
        "client_last_seen": datetime.now(timezone.utc).isoformat(),
    }
    return {"ok": True, "station": station}


@app.get("/api/station_alerts")
def get_station_alerts(vtec_thr: float = 80.0, roti_thr: float = 0.4, s4c_thr: float = 0.25):
    rows: List[Dict] = []
    for st in list_station_ids():
        m = STATION_LATEST.get(st, {})
        max_vtec = _float_or_none(m.get("max_vtec"))
        max_roti = _float_or_none(m.get("max_roti"))
        max_s4c = _float_or_none(m.get("max_s4c"))
        exceed_vtec = bool(max_vtec is not None and max_vtec >= vtec_thr)
        exceed_roti = bool(max_roti is not None and max_roti >= roti_thr)
        exceed_s4c = bool(max_s4c is not None and max_s4c >= s4c_thr)
        rows.append({
            "station": st,
            "last_at": m.get("last_at"),
            "rows": int(m.get("rows", 0)),
            "max_vtec": max_vtec,
            "max_roti": max_roti,
            "max_s4c": max_s4c,
            "exceed": {
                "vtec": exceed_vtec,
                "roti": exceed_roti,
                "s4c": exceed_s4c,
                "any": bool(exceed_vtec or exceed_roti or exceed_s4c),
            },
        })

    rows.sort(key=lambda r: (not r["exceed"]["any"], r["station"]))
    exceeded = [r for r in rows if r["exceed"]["any"]]
    return {
        "ok": True,
        "thresholds": {
            "vtec": vtec_thr,
            "roti": roti_thr,
            "s4c": s4c_thr,
        },
        "rows": rows,
        "exceeded": exceeded,
    }


@app.get("/api/phase2/events")
def get_phase2_events(
    station: str,
    date: Optional[str] = None,
    roti_thr: float = 0.4,
    max_events: int = 2400,
    enable_ai: bool = False,
    all_stations: bool = False,
    user_lat: Optional[float] = None,
    user_lon: Optional[float] = None,
    user_radius_km: float = 650.0,
):
    station_norm = normalize_station_id(station)
    if not station_norm:
        return {"ok": False, "reason": "missing station", "events": [], "segments": [], "report": []}

    date_use = (date or utc_today_str()).strip()
    station_ids = list_station_ids() if bool(all_stations) else [station_norm]

    points_per_station = int(max(40, min(600, max_events // max(1, len(station_ids)))))
    src_rows: List[Dict] = []
    threshold_rows: List[Dict] = []
    for sid in station_ids:
        st = get_station(sid)
        if not st:
            continue
        df = load_station_day_roti_csv(sid, date_use)
        if df is None or df.empty:
            continue

        # Plot all recent values for each station (not only values above threshold).
        src_use = df.sort_values("ts", ascending=False).head(points_per_station)
        src_thr = src_use[src_use["ROTI"] >= roti_thr]

        for _, r in src_use.iterrows():
            src_rows.append(
                {
                    "station": sid,
                    "ts": str(r["ts"]),
                    "prn": str(r["prn"]),
                    "roti": float(r["ROTI"]),
                    "st_lat": float(st["lat"]),
                    "st_lon": float(st["lon"]),
                }
            )
        for _, r in src_thr.iterrows():
            threshold_rows.append(
                {
                    "station": sid,
                    "ts": str(r["ts"]),
                    "prn": str(r["prn"]),
                    "roti": float(r["ROTI"]),
                    "st_lat": float(st["lat"]),
                    "st_lon": float(st["lon"]),
                }
            )

    if not src_rows:
        return {
            "ok": True,
            "station": station_norm,
            "scope": "all" if bool(all_stations) else "single",
            "date": date_use,
            "thresholds": {"roti": roti_thr, "user_radius_km": user_radius_km},
            "events": [],
            "segments": [],
            "report": [],
            "ai": {"enabled": bool(enable_ai), "active": False, "source": "none"},
            "nearby": {"enabled": False, "prns": [], "events": []},
            "location_advisory": {"enabled": False, "in_risk_zone": False, "forbidden_prns": []},
        }

    max_events = int(max(10, min(800, max_events)))
    roti_thr = float(max(0.05, min(3.0, roti_thr)))
    user_radius_km = float(max(20.0, min(2500.0, user_radius_km)))
    src_df = pd.DataFrame(src_rows)
    src_df = src_df.sort_values("ts", ascending=False).head(max_events)

    thr_df = pd.DataFrame(threshold_rows) if threshold_rows else pd.DataFrame(columns=["station", "ts", "prn", "roti", "st_lat", "st_lon"])
    if not thr_df.empty:
        thr_df = thr_df.sort_values(["roti", "ts"], ascending=[False, False]).head(max_events)

    events: List[Dict] = []
    for i, r in enumerate(src_df.itertuples(index=False), start=1):
        roti = float(r.roti)
        ts = str(r.ts)
        prn = str(r.prn)
        sid = str(r.station)
        ipp = _proxy_ipp(float(r.st_lat), float(r.st_lon), prn, ts, roti)
        sev = _severity_from_roti(roti)
        events.append(
            {
                "id": f"{sid}-{prn}-{ts}-{i}",
                "station": sid,
                "ts": ts,
                "prn": prn,
                "roti": round(roti, 4),
                "lat": round(ipp["lat"], 6),
                "lon": round(ipp["lon"], 6),
                "severity": sev,
            }
        )

    events.sort(key=lambda x: x["ts"], reverse=True)

    threshold_events: List[Dict] = []
    for i, r in enumerate(thr_df.itertuples(index=False), start=1):
        roti = float(r.roti)
        ts = str(r.ts)
        prn = str(r.prn)
        sid = str(r.station)
        ipp = _proxy_ipp(float(r.st_lat), float(r.st_lon), prn, ts, roti)
        threshold_events.append(
            {
                "id": f"thr-{sid}-{prn}-{ts}-{i}",
                "station": sid,
                "ts": ts,
                "prn": prn,
                "roti": round(roti, 4),
                "lat": round(ipp["lat"], 6),
                "lon": round(ipp["lon"], 6),
                "severity": _severity_from_roti(roti),
            }
        )

    threshold_events.sort(key=lambda x: (x["severity"] != "high", -float(x["roti"]), x["prn"]))
    ai_meta = {"enabled": bool(enable_ai), "active": False, "source": "heuristic"}
    segments = []
    if bool(enable_ai) and threshold_events:
        ai_res = _run_roboflow_segmentation(
            threshold_events,
            station=station_norm,
            date=date_use,
            roti_thr=roti_thr,
            user_lat=user_lat,
            user_lon=user_lon,
            user_radius_km=user_radius_km,
        )
        if ai_res.get("ok") and ai_res.get("segments"):
            segments = ai_res.get("segments")
            ai_meta = {
                "enabled": True,
                "active": True,
                "source": "roboflow-white-canvas",
                "cached": bool(ai_res.get("cached")),
            }
        else:
            segments = _segment_overlays(threshold_events)
            ai_meta = {
                "enabled": True,
                "active": False,
                "source": "heuristic-fallback",
                "reason": ai_res.get("reason", "ai unavailable"),
            }
    else:
        segments = _segment_overlays(threshold_events)

    nearby_enabled = user_lat is not None and user_lon is not None
    nearby_events: List[Dict] = []
    nearby_prns: List[str] = []
    if nearby_enabled:
        ulat = _clamp_lat(float(user_lat))
        ulon = _wrap_lon(float(user_lon))
        for e in events:
            dist = _haversine_km(ulat, ulon, float(e["lat"]), float(e["lon"]))
            if dist <= user_radius_km:
                item = {**e, "distance_km": round(dist, 1)}
                nearby_events.append(item)
        nearby_events.sort(key=lambda x: (x["severity"] != "high", x["distance_km"], -float(x["roti"])))
        nearby_prns = sorted({str(e["prn"]) for e in nearby_events})

    location_advisory = {"enabled": nearby_enabled, "in_risk_zone": False, "forbidden_prns": []}
    if nearby_enabled:
        ulat = _clamp_lat(float(user_lat))
        ulon = _wrap_lon(float(user_lon))
        risk_segments = [s for s in segments if str(s.get("severity")) in ("high", "medium")]
        in_zone = any(_point_in_polygon(ulat, ulon, s.get("polygon") or []) for s in risk_segments)
        if in_zone:
            forbidden = sorted(
                {
                    str(e.get("prn"))
                    for e in nearby_events
                    if str(e.get("severity")) in ("high", "medium")
                }
            )
            location_advisory = {
                "enabled": True,
                "in_risk_zone": True,
                "forbidden_prns": forbidden,
                "reason": "location intersects segmented high/medium risk zone",
            }
        else:
            location_advisory = {
                "enabled": True,
                "in_risk_zone": False,
                "forbidden_prns": [],
                "reason": "location outside segmented risk zones",
            }

    report = []
    for e in events[:8]:
        report.append(
            {
                "id": e["id"],
                "title": f"{e['station']} PRN {e['prn']} ROTI {float(e['roti']):.2f}",
                "severity": e["severity"],
                "time": e["ts"],
                "area": f"{e['station']} iono-pierce proxy",
            }
        )

    return {
        "ok": True,
        "station": station_norm,
        "scope": "all" if bool(all_stations) else "single",
        "date": date_use,
        "thresholds": {
            "roti": roti_thr,
            "user_radius_km": user_radius_km,
        },
        "events": events,
        "segments": segments,
        "report": report,
        "ai": ai_meta,
        "nearby": {
            "enabled": nearby_enabled,
            "user_lat": round(float(user_lat), 6) if nearby_enabled else None,
            "user_lon": round(float(user_lon), 6) if nearby_enabled else None,
            "radius_km": user_radius_km,
            "prns": nearby_prns,
            "events": nearby_events[:20],
        },
        "location_advisory": location_advisory,
    }

# ----------------------------
# Publish endpoint: worker -> backend
# ----------------------------
@app.post("/api/publish")
async def publish(payload: Dict = Body(...)):
    """
    Expected JSON:
      {"station": "KMIT6",
       "data": [ {ts, prn, S4c, ROTI, VTEC}, ... ] }
    """
    station = normalize_station_id(payload.get("station"))
    data = payload.get("data")

    if not station or not isinstance(data, list):
        return {"ok": False, "reason": "invalid payload"}

    if len(data) > PUBLISH_MAX_ROWS:
        data = data[-PUBLISH_MAX_ROWS:]

    # ✅ filter SBAS/OTHER/unknown here too + de-dup (ts, prn)
    dedup: Dict[tuple, Dict] = {}
    for row in data:
        prn = str(row.get("prn", "")).strip()
        if not is_allowed_prn(prn):
            continue
        key = (row.get("ts"), prn)
        stec = _filter_range(row.get("STEC"), STEC_MIN, STEC_MAX)
        vtec = _filter_range(row.get("VTEC"), VTEC_MIN, VTEC_MAX)
        dedup[key] = {
            "ts": row.get("ts"),
            "prn": prn,
            "S4c": row.get("S4c"),
            "ROTI": row.get("ROTI"),
            "VTEC": vtec,
            "STEC": stec,
            "station": station,
        }

    filtered = list(dedup.values())

    if not filtered:
        return {"ok": True, "written": 0, "broadcast": 0}

    vtecs = [v for v in (_float_or_none(r.get("VTEC")) for r in filtered) if v is not None]
    rotis = [v for v in (_float_or_none(r.get("ROTI")) for r in filtered) if v is not None]
    s4cs = [v for v in (_float_or_none(r.get("S4c")) for r in filtered) if v is not None]
    STATION_LATEST[station] = {
        "last_at": datetime.now(timezone.utc).isoformat(),
        "rows": len(filtered),
        "max_vtec": max(vtecs) if vtecs else None,
        "max_roti": max(rotis) if rotis else None,
        "max_s4c": max(s4cs) if s4cs else None,
    }

    t0 = time.perf_counter()

    # write daily csv by row date (offload file I/O to thread pool)
    by_date: Dict[str, List[Dict]] = {}
    for r in filtered:
        d = date_from_ts(r.get("ts", ""))
        by_date.setdefault(d, []).append(r)
    write_tasks = []
    for d, rows in by_date.items():
        csv_path = ensure_daily_csv(station, d)
        write_tasks.append(asyncio.to_thread(append_rows_to_csv, csv_path, rows))
    if write_tasks:
        await asyncio.gather(*write_tasks)

    t_after_write = time.perf_counter()

    # broadcast to ws listeners
    msg = json.dumps({"station": station, "data": filtered})
    await manager.broadcast(station, msg)
    t_after_broadcast = time.perf_counter()

    write_ms = (t_after_write - t0) * 1000.0
    broadcast_ms = (t_after_broadcast - t_after_write) * 1000.0
    total_ms = (t_after_broadcast - t0) * 1000.0
    publish_metrics = RUNTIME_METRICS["publish"]
    publish_metrics["last_station"] = station
    publish_metrics["last_rows"] = len(filtered)
    publish_metrics["last_write_ms"] = round(write_ms, 3)
    publish_metrics["last_broadcast_ms"] = round(broadcast_ms, 3)
    publish_metrics["last_total_ms"] = round(total_ms, 3)
    publish_metrics["last_at"] = datetime.now(timezone.utc).isoformat()
    publish_metrics["count"] = int(publish_metrics.get("count", 0)) + 1

    return {
        "ok": True,
        "written": len(filtered),
        "broadcast": len(filtered),
        "latency_ms": {
            "write": round(write_ms, 3),
            "broadcast": round(broadcast_ms, 3),
            "total": round(total_ms, 3),
        },
    }
