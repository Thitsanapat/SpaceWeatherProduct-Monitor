#!/usr/bin/env python3
"""Poll backend runtime metrics and write CSV for long-run soak tests."""

import argparse
import csv
import datetime as dt
import json
import time
from pathlib import Path
from typing import Any, Dict

import requests


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Collect /api/runtime_metrics into CSV over time")
    p.add_argument("--backend", default="http://localhost:8000", help="Backend base URL")
    p.add_argument("--station", default="KMIT6", help="Station ID")
    p.add_argument("--interval-sec", type=float, default=2.0, help="Polling interval")
    p.add_argument("--duration-hr", type=float, default=6.0, help="Duration in hours")
    p.add_argument("--out", default="soak_metrics.csv", help="Output CSV path")
    return p.parse_args()


def flatten_metrics(payload: Dict[str, Any], station: str) -> Dict[str, Any]:
    ws = payload.get("ws", {}) if isinstance(payload, dict) else {}
    pub = payload.get("publish", {}) if isinstance(payload, dict) else {}
    client = payload.get("client", {}) if isinstance(payload, dict) else {}
    if isinstance(client, dict) and station in client and isinstance(client[station], dict):
        client = client[station]

    return {
        "ts_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "ws_total_connections": ws.get("total_connections"),
        "ws_station_connections": ws.get("station_connections"),
        "publish_count": pub.get("count"),
        "publish_rows": pub.get("last_rows"),
        "publish_write_ms": pub.get("last_write_ms"),
        "publish_broadcast_ms": pub.get("last_broadcast_ms"),
        "publish_total_ms": pub.get("last_total_ms"),
        "client_ws_status": client.get("ws_status") if isinstance(client, dict) else None,
        "client_ws_reconnects": client.get("ws_reconnects") if isinstance(client, dict) else None,
        "client_pending_samples": client.get("pending_samples") if isinstance(client, dict) else None,
        "client_pending_counts": client.get("pending_counts") if isinstance(client, dict) else None,
        "client_render_ms": client.get("render_ms") if isinstance(client, dict) else None,
        "client_heap_mb": client.get("heap_mb") if isinstance(client, dict) else None,
        "client_fps": client.get("fps") if isinstance(client, dict) else None,
        "client_last_seen": client.get("client_last_seen") if isinstance(client, dict) else None,
    }


def main() -> None:
    args = parse_args()
    out_path = Path(args.out).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    end_time = time.monotonic() + (args.duration_hr * 3600.0)
    fieldnames = [
        "ts_utc",
        "ws_total_connections",
        "ws_station_connections",
        "publish_count",
        "publish_rows",
        "publish_write_ms",
        "publish_broadcast_ms",
        "publish_total_ms",
        "client_ws_status",
        "client_ws_reconnects",
        "client_pending_samples",
        "client_pending_counts",
        "client_render_ms",
        "client_heap_mb",
        "client_fps",
        "client_last_seen",
    ]

    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()

        while time.monotonic() < end_time:
            url = f"{args.backend.rstrip('/')}/api/runtime_metrics"
            try:
                r = requests.get(url, params={"station": args.station}, timeout=3)
                r.raise_for_status()
                payload = r.json()
            except Exception as exc:  # keep collecting even if backend has transient errors
                payload = {"ws": {}, "publish": {}, "client": {}, "error": str(exc)}

            row = flatten_metrics(payload, args.station)
            writer.writerow(row)
            f.flush()
            print(json.dumps(row, ensure_ascii=True))
            time.sleep(max(0.1, args.interval_sec))

    print(f"Soak metrics saved to: {out_path}")


if __name__ == "__main__":
    main()
