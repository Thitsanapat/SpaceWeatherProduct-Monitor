#!/usr/bin/env python3
"""Stream a station/day CSV to backend `/api/publish` to simulate realtime.

Usage:
  python backend/scripts/stream_csv_to_publish.py --station KMIT6 --date 2026-02-05 --speed 1.0

If `--date` is omitted, today's UTC date is used. `--speed` scales replay speed (1.0 = realtime).
"""
from pathlib import Path
import argparse, time, json
from datetime import datetime, timezone
import requests
import pandas as pd

BASE = Path(__file__).resolve().parents[1]
DATA_DIR = BASE / 'data'

def utc_today_str():
    return datetime.now(timezone.utc).strftime('%Y-%m-%d')

def load_csv(station, date_str):
    p = DATA_DIR / station / f"{date_str}.csv"
    if not p.exists():
        raise FileNotFoundError(p)
    df = pd.read_csv(p, dtype=str)
    if 'ts' not in df.columns and 'UTC(HH:MM:SS)' in df.columns:
        df = df.rename(columns={
            'UTC(HH:MM:SS)':'ts',
            'PRN':'prn',
            'S4c':'S4c',
            'ROTI(TECU/min)':'ROTI',
            'VTEC(TECU)':'VTEC',
            'STEC(TECU)':'STEC',
        })
        df['ts'] = date_str + 'T' + df['ts'] + 'Z'
    df['ts'] = pd.to_datetime(df['ts'], errors='coerce', utc=True)
    df = df.dropna(subset=['ts','prn'])
    # coerce numeric
    for c in ('S4c','ROTI','VTEC','STEC'):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors='coerce')
        else:
            df[c] = pd.NA
    df = df.dropna(subset=['S4c','ROTI','VTEC','STEC'], how='all')
    df = df.sort_values('ts').reset_index(drop=True)
    return df

def stream(df, station, speed=1.0, batch_size=10, url='http://localhost:8000/api/publish'):
    # group by timestamp
    groups = df.groupby('ts', sort=True)
    prev = None
    session = requests.Session()
    for t, g in groups:
        if prev is not None:
            dt_sec = (t.to_pydatetime() - prev.to_pydatetime()).total_seconds()
            if dt_sec > 0:
                time.sleep(dt_sec / max(speed, 1e-6))
        prev = t
        rows = []
        for _, r in g.iterrows():
            rows.append({
                'ts': r['ts'].strftime('%Y-%m-%dT%H:%M:%SZ') if hasattr(r['ts'],'strftime') else str(r['ts']),
                'prn': str(r['prn']),
                'S4c': float(r['S4c']),
                'ROTI': float(r['ROTI']),
                'VTEC': float(r['VTEC']),
                'STEC': float(r['STEC']) if 'STEC' in r and pd.notna(r['STEC']) else None
            })

        # send in batches
        for i in range(0, len(rows), batch_size):
            chunk = rows[i:i+batch_size]
            payload = {'station': station, 'data': chunk}
            try:
                resp = session.post(url, json=payload, timeout=5)
                # optional: print response for debug
                print(datetime.now(timezone.utc).isoformat(), 'sent', len(chunk), 'rows ->', resp.status_code)
            except Exception as e:
                print('POST error', e)

    print('Streaming complete')

def main():
    p = argparse.ArgumentParser()
    p.add_argument('--station', default='KMIT6')
    p.add_argument('--date', default=utc_today_str())
    p.add_argument('--speed', type=float, default=5.0, help='Replay speed multiplier (1=realtime)')
    p.add_argument('--batch', type=int, default=10)
    args = p.parse_args()

    df = load_csv(args.station, args.date)
    stream(df, args.station, speed=args.speed, batch_size=args.batch)

if __name__ == '__main__':
    main()
