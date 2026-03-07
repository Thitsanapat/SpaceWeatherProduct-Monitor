# backend/main.py
# -*- coding: utf-8 -*-

import asyncio
import csv
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Body
from fastapi.middleware.cors import CORSMiddleware
from stations import DEFAULT_STATION_ID, list_stations, normalize_station_id

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
    for st, m in STATION_LATEST.items():
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
