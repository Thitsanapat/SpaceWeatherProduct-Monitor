# backend/main.py
# -*- coding: utf-8 -*-

import asyncio
import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Body
from fastapi.middleware.cors import CORSMiddleware
from stations import DEFAULT_STATION_ID, list_stations, normalize_station_id

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"   # backend/data/<station>/<date>.csv

CSV_HEADER = ["ts", "prn", "S4c", "ROTI", "VTEC", "station"]

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

manager = ConnectionManager()

# ----------------------------
# helpers
# ----------------------------
def utc_today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")

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
                r.get("station", ""),
            ])

def is_allowed_prn(prn: str) -> bool:
    if not prn:
        return False
    s = prn[0].upper()
    # ✅ ไม่เอา SBAS(S) และไม่เอา OTHER/unknown
    return s in ("G", "E", "C", "R", "J", "I")

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

    for col in ("S4c", "ROTI", "VTEC"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        else:
            df[col] = pd.NA

    df = df.dropna(subset=["S4c", "ROTI", "VTEC"])
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

    rows = df[["ts", "prn", "S4c", "ROTI", "VTEC"]].to_dict(orient="records")
    return {"ok": True, "rows": rows}

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
        # acknowledge registration so client knows it's ready
        await ws.send_text(json.dumps({"ok": True, "station": station, "msg": "registered"}))

        # keep alive loop (data มาจาก /api/publish)
        while True:
            await asyncio.sleep(10)

    except WebSocketDisconnect:
        await manager.unregister(station, ws)
        return
    except Exception:
        await manager.unregister(station, ws)
        return

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

    # ✅ filter SBAS/OTHER/unknown here too
    filtered = []
    for row in data:
        prn = str(row.get("prn", "")).strip()
        if not is_allowed_prn(prn):
            continue
        filtered.append({
            "ts": row.get("ts"),
            "prn": prn,
            "S4c": row.get("S4c"),
            "ROTI": row.get("ROTI"),
            "VTEC": row.get("VTEC"),
            "station": station,
        })

    if not filtered:
        return {"ok": True, "written": 0, "broadcast": 0}

    # write today csv
    today = utc_today_str()
    csv_path = ensure_daily_csv(station, today)
    append_rows_to_csv(csv_path, filtered)

    # broadcast to ws listeners
    msg = json.dumps({"station": station, "data": filtered})
    await manager.broadcast(station, msg)

    return {"ok": True, "written": len(filtered), "broadcast": len(filtered)}
