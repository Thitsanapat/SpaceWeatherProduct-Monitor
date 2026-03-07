"use client";

import React, { useEffect, useMemo, useRef, useState } from "react";
import {
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import Phase2Cesium from "./components/Phase2Cesium";

type Station = { id: string; name: string; lat?: number; lon?: number; alt?: number };

type Sample = {
  ts: string;
  prn: string;
  S4c: number | null;
  ROTI: number | null;
  VTEC: number | null;
  STEC: number | null;
};

type CountPoint = {
  ts: string;
  time: string;
  tod: number;
  GNSS: number;
  GPS?: number;
  GAL?: number;
  BDS?: number;
  GLO?: number;
  QZS?: number;
  SBAS?: number;
  IRNSS?: number;
};

type MetricKey = "VTEC" | "STEC" | "ROTI" | "S4c";

type MetricRow = {
  ts: string;
  time: string;
  tod: number;
  median: number | null;
  [key: string]: number | string | null;
};

type RealtimePayload = {
  type?: string;
  ts?: string;
  station?: string;
  data?: unknown;
  rows?: unknown;
  count?: unknown;
  counts?: unknown;
};

type RuntimeStats = {
  wsStatus: "closed" | "connecting" | "open" | "error";
  reconnectCount: number;
  pendingSamples: number;
  pendingCounts: number;
  fps: number;
  msgPerSec: number;
  flushMs: number;
  heapMb: number | null;
  lastHeartbeatAgoSec: number | null;
};

type StationAlertRow = {
  station: string;
  last_at: string | null;
  rows: number;
  max_vtec: number | null;
  max_roti: number | null;
  max_s4c: number | null;
  exceed: { vtec: boolean; roti: boolean; s4c: boolean; any: boolean };
};

const BACKEND = (process.env.NEXT_PUBLIC_BACKEND_URL ?? "http://localhost:8000")
  .trim()
  .replace(/\s+/g, "")
  .replace(/\/+$/, "");
const BACKEND_WS = BACKEND.replace(/^https?/i, (m) => (m.toLowerCase() === "https" ? "wss" : "ws"));
const REALTIME_WINDOW_MS = 1 * 60 * 60 * 1000;
const MAX_POINTS_PER_PRN = 6000;
const MAX_COUNT_POINTS = 6000;
const WS_FLUSH_INTERVAL_MS = 250;
const WS_RECONNECT_MS = 1500;
const WS_HEARTBEAT_TIMEOUT_MS = 15000;
const CLIENT_METRIC_PUSH_MS = 5000;
const DAY_START_SEC = 0;
const DAY_END_SEC = 24 * 3600;
const X_TICKS_SEC = [
  0,
  2 * 3600,
  4 * 3600,
  6 * 3600,
  8 * 3600,
  10 * 3600,
  12 * 3600,
  14 * 3600,
  16 * 3600,
  18 * 3600,
  20 * 3600,
  22 * 3600,
  DAY_END_SEC,
];

const C_BLUE = "#2563eb";
const C_ORANGE = "#f97316";
const C_GRAY = "#64748b";

function toHHMMSS(iso: string) {
  try {
    return new Date(iso).toISOString().slice(11, 19);
  } catch {
    return iso;
  }
}

function utcSecondsOfDay(iso: string) {
  const d = new Date(iso);
  if (!Number.isFinite(d.getTime())) return NaN;
  return d.getUTCHours() * 3600 + d.getUTCMinutes() * 60 + d.getUTCSeconds();
}

function formatUtcTick(sec: number) {
  const x = Math.max(0, Math.min(DAY_END_SEC, Math.floor(sec)));
  if (x === DAY_END_SEC) return "00:00";
  const hh = String(Math.floor(x / 3600)).padStart(2, "0");
  const mm = String(Math.floor((x % 3600) / 60)).padStart(2, "0");
  return `${hh}:${mm}`;
}

function formatUtcLabel(sec: number) {
  const x = Math.max(0, Math.min(DAY_END_SEC, Math.floor(sec)));
  if (x === DAY_END_SEC) return "00:00:00";
  const hh = String(Math.floor(x / 3600)).padStart(2, "0");
  const mm = String(Math.floor((x % 3600) / 60)).padStart(2, "0");
  const ss = String(x % 60).padStart(2, "0");
  return `${hh}:${mm}:${ss}`;
}

function toNum(v: unknown): number | null {
  if (v === null || v === undefined || v === "") return null;
  const n = typeof v === "number" ? v : Number(v);
  return Number.isFinite(n) ? n : null;
}

function sysOfPRN(prn: string) {
  const s = prn?.[0] ?? "O";
  if (s === "G") return "GPS";
  if (s === "E") return "GAL";
  if (s === "C") return "BDS";
  if (s === "R") return "GLO";
  if (s === "J") return "QZS";
  if (s === "I") return "IRNSS";
  return null;
}

function normalizeSample(s: unknown): Sample {
  const row = (s ?? {}) as Record<string, unknown>;
  return {
    ts: String(row.ts ?? ""),
    prn: String(row.prn ?? ""),
    S4c: toNum(row.S4c),
    ROTI: toNum(row.ROTI),
    VTEC: toNum(row.VTEC),
    STEC: toNum(row.STEC ?? row.stec),
  };
}

function mergeSample(prev: Sample | null | undefined, next: Sample): Sample {
  return {
    ts: next.ts,
    prn: next.prn,
    S4c: next.S4c ?? prev?.S4c ?? null,
    ROTI: next.ROTI ?? prev?.ROTI ?? null,
    VTEC: next.VTEC ?? prev?.VTEC ?? null,
    STEC: next.STEC ?? prev?.STEC ?? null,
  };
}

function pruneSamples(rows: Sample[], cutoffMs: number, maxPoints: number) {
  let firstKeep = 0;
  while (firstKeep < rows.length) {
    const tsMs = Date.parse(rows[firstKeep].ts);
    if (!Number.isFinite(tsMs) || tsMs >= cutoffMs) break;
    firstKeep += 1;
  }
  const byCutoff = firstKeep > 0 ? rows.slice(firstKeep) : rows;
  return byCutoff.length > maxPoints ? byCutoff.slice(byCutoff.length - maxPoints) : byCutoff;
}

function pruneCountSeries(rows: CountPoint[], cutoffMs: number, maxPoints: number) {
  let firstKeep = 0;
  while (firstKeep < rows.length) {
    const tsMs = Date.parse(rows[firstKeep].ts);
    if (!Number.isFinite(tsMs) || tsMs >= cutoffMs) break;
    firstKeep += 1;
  }
  const byCutoff = firstKeep > 0 ? rows.slice(firstKeep) : rows;
  return byCutoff.length > maxPoints ? byCutoff.slice(byCutoff.length - maxPoints) : byCutoff;
}

function median(arr: number[]) {
  const x = arr.filter((n) => Number.isFinite(n)).sort((a, b) => a - b);
  if (!x.length) return NaN;
  const m = Math.floor(x.length / 2);
  return x.length % 2 ? x[m] : 0.5 * (x[m - 1] + x[m]);
}

function buildMetricSeries(metric: MetricKey, prns: string[], series: Record<string, Sample[]>) {
  const tsSet = new Set<string>();
  const perPrn: Record<string, Record<string, number | null>> = {};

  for (const prn of prns) {
    const rows = series[prn] ?? [];
    const map: Record<string, number | null> = {};
    for (const s of rows) {
      tsSet.add(s.ts);
      map[s.ts] = s[metric] ?? null;
    }
    perPrn[prn] = map;
  }

  const tsList = Array.from(tsSet).sort();
  return tsList.map((ts) => {
    const tod = utcSecondsOfDay(ts);
    const row: MetricRow = { ts, time: toHHMMSS(ts), tod: Number.isFinite(tod) ? tod : 0, median: null };
    const vals: number[] = [];
    for (const prn of prns) {
      const v = perPrn[prn]?.[ts] ?? null;
      row[prn] = v;
      if (Number.isFinite(v as number)) vals.push(v as number);
    }
    const med = median(vals);
    row.median = Number.isFinite(med) ? med : null;
    return row;
  });
}

function fmtVal(v: unknown, digits = 6) {
  if (typeof v !== "number" || !Number.isFinite(v)) return "-";
  return v.toFixed(digits);
}

function CompactTooltip({
  active,
  payload,
  label,
  title,
}: {
  active?: boolean;
  payload?: Array<{ dataKey?: unknown; value?: unknown; color?: string }>;
  label?: unknown;
  title: string;
}) {
  if (!active || !payload || !payload.length) return null;

  const medianItem = payload.find((p) => String(p.dataKey) === "median" && Number.isFinite(p.value));
  const items = payload
    .filter((p) => String(p.dataKey) !== "median" && Number.isFinite(p.value))
    .map((p) => ({
      key: String(p.dataKey),
      value: p.value as number,
      color: p.color as string,
    }))
    .sort((a, b) => a.key.localeCompare(b.key));

  return (
    <div className="rounded-lg border border-slate-200 bg-white/95 shadow-lg p-2 text-xs max-w-[240px]">
      <div className="font-semibold">{title}</div>
      <div className="text-slate-500">{typeof label === "number" ? formatUtcLabel(label) : String(label ?? "")}</div>
      {medianItem ? (
        <div className="mt-1">
          <span className="text-slate-500">median:</span> {fmtVal(medianItem.value)}
        </div>
      ) : null}
      <div className="mt-1 pr-1">
        {items.map((it) => (
          <div key={it.key} className="flex items-center gap-2">
            <span className="inline-block h-2 w-2 rounded-full" style={{ background: it.color }} />
            <span className="truncate">{it.key}</span>
            <span className="ml-auto">{fmtVal(it.value)}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

function Card({
  title,
  subtitle,
  children,
}: {
  title: string;
  subtitle?: string;
  children: React.ReactNode;
}) {
  return (
    <section className="bg-white border border-slate-200 rounded-2xl p-4 shadow-sm">
      <div className="flex flex-col gap-1 mb-3">
        <div className="text-lg font-semibold">{title}</div>
        {subtitle ? <div className="text-sm text-slate-600">{subtitle}</div> : null}
      </div>
      {children}
    </section>
  );
}

function ChartBox({
  height = 350,
  children,
}: {
  height?: number;
  children: React.ReactNode;
}) {
  return <div style={{ width: "100%", height, minHeight: height, minWidth: 0 }}>{children}</div>;
}

function RealtimeHealth({ stats }: { stats: RuntimeStats }) {
  const tone =
    stats.wsStatus === "open" ? "text-emerald-700 bg-emerald-50" :
    stats.wsStatus === "connecting" ? "text-amber-700 bg-amber-50" :
    "text-rose-700 bg-rose-50";

  const heartbeatText =
    stats.lastHeartbeatAgoSec === null ? "-" : `${stats.lastHeartbeatAgoSec.toFixed(1)}s ago`;

  return (
    <section className="bg-white border border-slate-200 rounded-2xl p-4 shadow-sm">
      <div className="flex items-center justify-between gap-3 mb-3">
        <div className="text-lg font-semibold">Realtime Health</div>
        <span className={`px-2 py-1 rounded-full text-xs font-medium ${tone}`}>
          WS: {stats.wsStatus}
        </span>
      </div>
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3 text-sm">
        <div className="rounded-lg border border-slate-200 p-2"><div className="text-slate-500">Reconnects</div><div className="font-semibold">{stats.reconnectCount}</div></div>
        <div className="rounded-lg border border-slate-200 p-2"><div className="text-slate-500">Msg/s</div><div className="font-semibold">{stats.msgPerSec.toFixed(1)}</div></div>
        <div className="rounded-lg border border-slate-200 p-2"><div className="text-slate-500">FPS</div><div className="font-semibold">{stats.fps.toFixed(1)}</div></div>
        <div className="rounded-lg border border-slate-200 p-2"><div className="text-slate-500">Flush ms</div><div className="font-semibold">{stats.flushMs.toFixed(2)}</div></div>
        <div className="rounded-lg border border-slate-200 p-2"><div className="text-slate-500">Pending samples</div><div className="font-semibold">{stats.pendingSamples}</div></div>
        <div className="rounded-lg border border-slate-200 p-2"><div className="text-slate-500">Pending counts</div><div className="font-semibold">{stats.pendingCounts}</div></div>
        <div className="rounded-lg border border-slate-200 p-2"><div className="text-slate-500">Heap MB</div><div className="font-semibold">{stats.heapMb === null ? "-" : stats.heapMb.toFixed(1)}</div></div>
        <div className="rounded-lg border border-slate-200 p-2"><div className="text-slate-500">Heartbeat</div><div className="font-semibold">{heartbeatText}</div></div>
      </div>
    </section>
  );
}

function StationThresholdBoard({ rows }: { rows: StationAlertRow[] }) {
  const exceeded = rows.filter((r) => r.exceed.any);
  return (
    <section className="bg-white border border-slate-200 rounded-2xl p-4 shadow-sm">
      <div className="flex items-center justify-between gap-3 mb-3">
        <div className="text-lg font-semibold">Stations Exceeding Threshold</div>
        <span className="text-xs rounded-full px-2 py-1 bg-slate-100 text-slate-600">
          {exceeded.length} exceeded
        </span>
      </div>

      {!rows.length ? (
        <div className="text-sm text-slate-500">No realtime station alerts yet.</div>
      ) : (
        <div className="overflow-auto">
          <table className="min-w-full text-sm">
            <thead>
              <tr className="text-left text-slate-500 border-b border-slate-200">
                <th className="py-2 pr-4">Station</th>
                <th className="py-2 pr-4">VTEC max</th>
                <th className="py-2 pr-4">ROTI max</th>
                <th className="py-2 pr-4">S4c max</th>
                <th className="py-2 pr-4">Last update</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.station} className="border-b border-slate-100">
                  <td className="py-2 pr-4 font-medium text-slate-900">{r.station}</td>
                  <td className="py-2 pr-4">
                    <span className={r.exceed.vtec ? "text-rose-600 font-semibold" : "text-slate-700"}>
                      {r.max_vtec === null ? "-" : r.max_vtec.toFixed(2)}
                    </span>
                  </td>
                  <td className="py-2 pr-4">
                    <span className={r.exceed.roti ? "text-rose-600 font-semibold" : "text-slate-700"}>
                      {r.max_roti === null ? "-" : r.max_roti.toFixed(3)}
                    </span>
                  </td>
                  <td className="py-2 pr-4">
                    <span className={r.exceed.s4c ? "text-rose-600 font-semibold" : "text-slate-700"}>
                      {r.max_s4c === null ? "-" : r.max_s4c.toFixed(3)}
                    </span>
                  </td>
                  <td className="py-2 pr-4 text-slate-500">{r.last_at ? toHHMMSS(r.last_at) : "-"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

export default function Page() {
  const [stations, setStations] = useState<Station[]>([]);
  const [station, setStation] = useState("KMIT6");
  const [elevCut, setElevCut] = useState(30);
  const elevDebounce = useRef<number | null>(null);

  const today = new Date().toISOString().slice(0, 10);
  const [selectedDate, setSelectedDate] = useState(today);
  const isRealtime = selectedDate === today;

  const [series, setSeries] = useState<Record<string, Sample[]>>({});
  const [countSeries, setCountSeries] = useState<CountPoint[]>([]);

  const wsRef = useRef<WebSocket | null>(null);
  const pendingSamplesRef = useRef<Sample[]>([]);
  const pendingCountsRef = useRef<Array<Record<string, unknown>>>([]);
  const flushTimerRef = useRef<number | null>(null);
  const wsStatusRef = useRef<RuntimeStats["wsStatus"]>("closed");
  const reconnectCountRef = useRef(0);
  const msgPerSecRef = useRef(0);
  const msgCounterRef = useRef(0);
  const fpsRef = useRef(0);
  const flushMsRef = useRef(0);
  const heapMbRef = useRef<number | null>(null);
  const lastHeartbeatAtRef = useRef<number | null>(null);

  const [runtimeStats, setRuntimeStats] = useState<RuntimeStats>({
    wsStatus: "closed",
    reconnectCount: 0,
    pendingSamples: 0,
    pendingCounts: 0,
    fps: 0,
    msgPerSec: 0,
    flushMs: 0,
    heapMb: null,
    lastHeartbeatAgoSec: null,
  });
  const [stationAlerts, setStationAlerts] = useState<StationAlertRow[]>([]);

  const stationAlertsAll = useMemo<StationAlertRow[]>(() => {
    const byStation = new Map<string, StationAlertRow>();
    for (const row of stationAlerts) byStation.set(row.station, row);

    const rows: StationAlertRow[] = stations.map((s) => {
      return (
        byStation.get(s.id) ?? {
          station: s.id,
          last_at: null,
          rows: 0,
          max_vtec: null,
          max_roti: null,
          max_s4c: null,
          exceed: { vtec: false, roti: false, s4c: false, any: false },
        }
      );
    });

    for (const row of stationAlerts) {
      if (!rows.some((r) => r.station === row.station)) rows.push(row);
    }

    rows.sort((a, b) => {
      if (a.exceed.any !== b.exceed.any) return a.exceed.any ? -1 : 1;
      return a.station.localeCompare(b.station);
    });
    return rows;
  }, [stations, stationAlerts]);

  useEffect(() => {
    let alive = true;
    fetch(`${BACKEND}/api/stations`)
      .then((r) => r.json())
      .then((j) => {
        if (!alive) return;
        const list: Station[] = j.stations ?? [];
        setStations(list);
        setStation((prev) => (list.some((s) => s.id === prev) ? prev : (list[0]?.id ?? prev)));
      })
      .catch(() => {
        if (!alive) return;
        const fallback: Station[] = [{ id: "KMIT6", name: "KMITL Station (Urban)" }];
        setStations(fallback);
        setStation("KMIT6");
      });
    return () => {
      alive = false;
    };
  }, []);

  useEffect(() => {
    setSeries({});
    setCountSeries([]);
  }, [station, selectedDate]);

  useEffect(() => {
    fetch(`${BACKEND}/api/worker_config?station=${encodeURIComponent(station)}`)
      .then((r) => r.json())
      .then((j) => {
        const v = j?.config?.elev_cut;
        if (typeof v === "number") setElevCut(v);
      })
      .catch(() => {});
  }, [station]);

  useEffect(() => {
    if (!isRealtime) return;
    if (elevDebounce.current) window.clearTimeout(elevDebounce.current);
    elevDebounce.current = window.setTimeout(() => {
      fetch(`${BACKEND}/api/worker_config`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ station, elev_cut: elevCut }),
      }).catch(() => {});
    }, 300);

    return () => {
      if (elevDebounce.current) window.clearTimeout(elevDebounce.current);
    };
  }, [elevCut, station, isRealtime]);

  useEffect(() => {
    if (!isRealtime) return;
    if (wsRef.current) wsRef.current.close();

    let closedByEffect = false;
    let reconnectTimer: number | null = null;

    const flushPending = () => {
      flushTimerRef.current = null;
      const flushStart = performance.now();

      const sampleBatch = pendingSamplesRef.current;
      pendingSamplesRef.current = [];
      if (sampleBatch.length) {
        setSeries((prev) => {
          const next = { ...prev };
          const grouped = new Map<string, Sample[]>();

          for (const s of sampleBatch) {
            const arr = grouped.get(s.prn);
            if (arr) arr.push(s);
            else grouped.set(s.prn, [s]);
          }

          for (const [prn, updates] of grouped) {
            const arr = next[prn] ? [...next[prn]] : [];
            for (const s of updates) {
              const last = arr.length ? arr[arr.length - 1] : null;
              if (last && last.ts === s.ts) arr[arr.length - 1] = mergeSample(last, s);
              else arr.push(s);
            }
            const latestMs = Date.parse(arr[arr.length - 1]?.ts ?? "");
            const cutoff = Number.isFinite(latestMs) ? latestMs - REALTIME_WINDOW_MS : Number.NEGATIVE_INFINITY;
            next[prn] = pruneSamples(arr, cutoff, MAX_POINTS_PER_PRN);
          }

          return next;
        });
      }

      const countBatch = pendingCountsRef.current;
      pendingCountsRef.current = [];
      if (countBatch.length) {
        setCountSeries((prev) => {
          const map = new Map(prev.map((r) => [r.ts, r]));
          for (const r of countBatch) {
            const ts = typeof r.ts === "string" ? r.ts : "";
            if (!ts) continue;
            const time = typeof r.time === "string" ? r.time : toHHMMSS(ts);
            map.set(ts, { ...(r as object), ts, time } as CountPoint);
          }
          const rows = Array.from(map.values()).sort((a, b) => a.ts.localeCompare(b.ts));
          const latestMs = Date.parse(rows[rows.length - 1]?.ts ?? "");
          const cutoff = Number.isFinite(latestMs) ? latestMs - REALTIME_WINDOW_MS : Number.NEGATIVE_INFINITY;
          return pruneCountSeries(rows, cutoff, MAX_COUNT_POINTS);
        });
      }

      flushMsRef.current = performance.now() - flushStart;
    };

    const scheduleFlush = () => {
      if (flushTimerRef.current !== null) return;
      flushTimerRef.current = window.setTimeout(flushPending, WS_FLUSH_INTERVAL_MS);
    };

    const connect = () => {
      const ws = new WebSocket(`${BACKEND_WS}/ws/realtime?station=${encodeURIComponent(station)}`);
      wsRef.current = ws;
      wsStatusRef.current = "connecting";

      ws.onopen = () => {
        wsStatusRef.current = "open";
        lastHeartbeatAtRef.current = Date.now();
        ws.send(JSON.stringify({ station }));
      };

      ws.onmessage = (ev) => {
        let msg: RealtimePayload | null = null;
        try {
          msg = JSON.parse(ev.data);
        } catch {
          return;
        }

        if (msg?.type === "heartbeat") {
          lastHeartbeatAtRef.current = Date.now();
          ws.send(JSON.stringify({ type: "ping", ts: new Date().toISOString() }));
          return;
        }
        if (msg?.type === "pong") {
          lastHeartbeatAtRef.current = Date.now();
          return;
        }

        msgCounterRef.current += 1;

        if (msg?.station && String(msg.station).toUpperCase() !== station.toUpperCase()) return;

        const dataRaw = Array.isArray(msg?.data)
          ? msg?.data
          : Array.isArray(msg?.rows)
          ? msg?.rows
          : msg?.data
          ? [msg?.data]
          : [];
        const data = dataRaw.map((row) => normalizeSample(row)).filter((s) => s.ts && s.prn);
        if (data.length) pendingSamplesRef.current.push(...data);

        const countRaw = Array.isArray(msg?.count)
          ? msg?.count
          : Array.isArray(msg?.counts)
          ? msg?.counts
          : msg?.count
          ? [msg?.count]
          : [];
        if (countRaw.length) pendingCountsRef.current.push(...countRaw.filter((r): r is Record<string, unknown> => !!r && typeof r === "object"));

        if (data.length || countRaw.length) scheduleFlush();
      };

      ws.onerror = () => {
        wsStatusRef.current = "error";
        ws.close();
      };

      ws.onclose = () => {
        if (wsRef.current === ws) wsRef.current = null;
        if (closedByEffect) return;
        wsStatusRef.current = "connecting";
        reconnectCountRef.current += 1;
        reconnectTimer = window.setTimeout(connect, WS_RECONNECT_MS);
      };
    };

    connect();

    return () => {
      closedByEffect = true;
      wsStatusRef.current = "closed";
      pendingSamplesRef.current = [];
      pendingCountsRef.current = [];
      if (flushTimerRef.current !== null) {
        window.clearTimeout(flushTimerRef.current);
        flushTimerRef.current = null;
      }
      if (reconnectTimer !== null) window.clearTimeout(reconnectTimer);
      wsRef.current?.close();
      wsRef.current = null;
    };
  }, [isRealtime, station]);

  useEffect(() => {
    if (!isRealtime) return;
    let alive = true;
    const poll = () => {
      fetch(`${BACKEND}/api/station_alerts?vtec_thr=80&roti_thr=0.4&s4c_thr=0.25`)
        .then((r) => r.json())
        .then((j) => {
          if (!alive) return;
          setStationAlerts(Array.isArray(j?.rows) ? j.rows : []);
        })
        .catch(() => {});
    };
    poll();
    const timer = window.setInterval(poll, 3000);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, [isRealtime]);

  useEffect(() => {
    if (!isRealtime) return;

    let rafId = 0;
    let frameCount = 0;
    let lastFpsTick = performance.now();
    let fpsNow = 0;

    const rafLoop = (now: number) => {
      frameCount += 1;
      if (now - lastFpsTick >= 1000) {
        fpsNow = (frameCount * 1000) / (now - lastFpsTick);
        fpsRef.current = fpsNow;
        frameCount = 0;
        lastFpsTick = now;
      }
      rafId = requestAnimationFrame(rafLoop);
    };
    rafId = requestAnimationFrame(rafLoop);

    const ticker = window.setInterval(() => {
      msgPerSecRef.current = msgCounterRef.current;
      msgCounterRef.current = 0;

      const perfAny = performance as Performance & { memory?: { usedJSHeapSize?: number } };
      heapMbRef.current = perfAny.memory?.usedJSHeapSize
        ? perfAny.memory.usedJSHeapSize / (1024 * 1024)
        : null;

      const hbMs = lastHeartbeatAtRef.current;
      const hbAgo = hbMs ? (Date.now() - hbMs) / 1000 : null;
      if (hbMs && Date.now() - hbMs > WS_HEARTBEAT_TIMEOUT_MS && wsRef.current?.readyState === WebSocket.OPEN) {
        wsRef.current.close();
      }

      setRuntimeStats({
        wsStatus: wsStatusRef.current,
        reconnectCount: reconnectCountRef.current,
        pendingSamples: pendingSamplesRef.current.length,
        pendingCounts: pendingCountsRef.current.length,
        fps: fpsRef.current,
        msgPerSec: msgPerSecRef.current,
        flushMs: flushMsRef.current,
        heapMb: heapMbRef.current,
        lastHeartbeatAgoSec: hbAgo,
      });
    }, 1000);

    const pushTimer = window.setInterval(() => {
      fetch(`${BACKEND}/api/client_metrics`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          station,
          render_ms: flushMsRef.current,
          heap_mb: heapMbRef.current,
          ws_reconnects: reconnectCountRef.current,
          pending_samples: pendingSamplesRef.current.length,
          pending_counts: pendingCountsRef.current.length,
          fps: fpsRef.current,
          ws_status: wsStatusRef.current,
        }),
      }).catch(() => {});
    }, CLIENT_METRIC_PUSH_MS);

    return () => {
      cancelAnimationFrame(rafId);
      window.clearInterval(ticker);
      window.clearInterval(pushTimer);
    };
  }, [isRealtime, station]);

  useEffect(() => {
    if (isRealtime) return;

    fetch(`${BACKEND}/api/history?station=${encodeURIComponent(station)}&date=${encodeURIComponent(selectedDate)}`)
      .then((r) => r.json())
      .then((j) => {
        if (!j.ok) return;
        const rows: Sample[] = Array.isArray(j.rows) ? j.rows.map(normalizeSample) : [];
        const grouped: Record<string, Sample[]> = {};
        for (const s of rows) {
          grouped[s.prn] ??= [];
          grouped[s.prn].push(s);
        }
        for (const prn of Object.keys(grouped)) {
          grouped[prn].sort((a, b) => a.ts.localeCompare(b.ts));
        }
        setSeries(grouped);
      })
      .catch(() => {});
  }, [isRealtime, station, selectedDate]);

  const prns = useMemo(() => Object.keys(series).sort(), [series]);

  const vtecSeries = useMemo(() => buildMetricSeries("VTEC", prns, series), [series, prns]);
  const stecSeries = useMemo(() => buildMetricSeries("STEC", prns, series), [series, prns]);
  const rotiSeries = useMemo(() => buildMetricSeries("ROTI", prns, series), [series, prns]);
  const s4cSeries = useMemo(() => buildMetricSeries("S4c", prns, series), [series, prns]);

  const emptyMetricSeries = useMemo(
    () => {
      const ts = new Date().toISOString();
      const tod = utcSecondsOfDay(ts);
      return [{ ts, time: toHHMMSS(ts), tod: Number.isFinite(tod) ? tod : 0, median: null }];
    },
    []
  );

  const satCount = useMemo<CountPoint[]>(() => {
    if (countSeries.length) return countSeries;
    const prnByTs: Record<string, Set<string>> = {};
    for (const prn of prns) {
      for (const s of series[prn] ?? []) {
        (prnByTs[s.ts] ??= new Set()).add(prn);
      }
    }

    const tsList = Object.keys(prnByTs).sort();
    return tsList.map((ts) => {
      const set = prnByTs[ts];
      const bySys: Record<string, number> = {};
      for (const p of set) {
        const sys = sysOfPRN(p);
        if (!sys) continue;
        bySys[sys] = (bySys[sys] ?? 0) + 1;
      }

      return {
        ts,
        time: toHHMMSS(ts),
        tod: Number.isFinite(utcSecondsOfDay(ts)) ? utcSecondsOfDay(ts) : 0,
        GNSS: set.size,
        GPS: bySys["GPS"] ?? 0,
        GAL: bySys["GAL"] ?? 0,
        BDS: bySys["BDS"] ?? 0,
        GLO: bySys["GLO"] ?? 0,
        QZS: bySys["QZS"] ?? 0,
        SBAS: bySys["SBAS"] ?? 0,
        IRNSS: bySys["IRNSS"] ?? 0,
      };
    });
  }, [series, prns, countSeries]);

  return (
    <div className="min-h-screen bg-white text-slate-900">
      <div className="max-w-6xl mx-auto p-6 space-y-6">
        <header className="space-y-1">
          <h1 className="text-2xl font-semibold">
            <span className="text-slate-900">GNSS</span>{" "}
            <span className="text-blue-600">Scintillation</span>{" "}
            <span className="text-orange-500">&</span>{" "}
            <span className="text-blue-600">Ionosphere</span> Monitor
          </h1>
          <p className="text-slate-600">
            Realtime / Post-process plots: <b>S4c</b>, <b>ROTI</b>, <b>STEC</b>, <b>TEC(VTEC)</b>, <b>Satellite count</b>
          </p>
        </header>

        <section className="bg-white border border-slate-200 rounded-2xl p-4 flex flex-wrap gap-4 items-end shadow-sm">
          <div className="flex flex-col gap-1">
            <label className="text-sm text-slate-600">Station</label>
            <select className="bg-white border border-slate-300 rounded-xl px-3 py-2" value={station} onChange={(e) => setStation(e.target.value)}>
              {stations.map((s) => (
                <option key={s.id} value={s.id}>
                  {s.id} - {s.name}
                </option>
              ))}
            </select>
          </div>

          <div className="flex flex-col gap-1">
            <label className="text-sm text-slate-600">Date</label>
            <input className="bg-white border border-slate-300 rounded-xl px-3 py-2" type="date" value={selectedDate} onChange={(e) => setSelectedDate(e.target.value)} />
            <div className="text-xs text-slate-500">{isRealtime ? "Realtime mode (Today)" : "Post-process mode (CSV)"}</div>
          </div>

          <div className="flex flex-col gap-1 min-w-[220px]">
            <label className="text-sm text-slate-600">Elevation cut (deg)</label>
            <div className="flex items-center gap-2">
              <input className="w-full" type="range" min={0} max={60} step={1} value={elevCut} onChange={(e) => setElevCut(Number(e.target.value))} disabled={!isRealtime} />
              <input className="w-16 bg-white border border-slate-300 rounded-lg px-2 py-1 text-sm" type="number" min={0} max={90} step={1} value={elevCut} onChange={(e) => setElevCut(Number(e.target.value))} disabled={!isRealtime} />
            </div>
            <div className="text-xs text-slate-500">{isRealtime ? "Applied to realtime stream" : "Realtime only (history unchanged)"}</div>
          </div>

          <div className="ml-auto text-sm text-slate-600">
            Active PRNs: <span className="text-slate-900 font-semibold">{prns.length}</span>
          </div>
        </section>

        {isRealtime ? <RealtimeHealth stats={runtimeStats} /> : null}
        {isRealtime ? <StationThresholdBoard rows={stationAlertsAll} /> : null}

        <div className="space-y-4">
          <Card title="GNSS TEC / VTEC (per-PRN + median)" subtitle="thin lines = per-PRN, thick line = median">
            <ChartBox height={350}>
              <ResponsiveContainer width="100%" height="100%" minHeight={300} minWidth={0}>
                <LineChart data={vtecSeries.length ? vtecSeries : emptyMetricSeries}>
                  <CartesianGrid stroke="#e2e8f0" />
                  <XAxis dataKey="tod" type="number" domain={[DAY_START_SEC, DAY_END_SEC]} ticks={X_TICKS_SEC} tickFormatter={formatUtcTick} label={{ value: "UTC Time", position: "insideBottom", offset: -4 }} />
                  <YAxis domain={[0, 100]} allowDataOverflow label={{ value: "TEC (TECU)", angle: -90, position: "insideLeft" }} />
                  <Tooltip content={<CompactTooltip title="VTEC" />} />
                  <Legend />
                  {prns.map((prn, idx) => (
                    <Line key={prn} type="monotone" dataKey={prn} name={prn} dot={false} stroke={idx % 3 === 0 ? "#0ea5e9" : "#fb7185"} strokeWidth={1} isAnimationActive={false} connectNulls />
                  ))}
                  <Line type="monotone" dataKey="median" name="median VTEC" dot={false} stroke={C_BLUE} strokeWidth={3} isAnimationActive={false} />
                </LineChart>
              </ResponsiveContainer>
            </ChartBox>
          </Card>

          <Card title="Slant TEC (STEC)" subtitle="code-smoothed (per-PRN + median)">
            <ChartBox height={350}>
              <ResponsiveContainer width="100%" height="100%" minHeight={300} minWidth={0}>
                <LineChart data={stecSeries.length ? stecSeries : emptyMetricSeries}>
                  <CartesianGrid stroke="#e2e8f0" />
                  <XAxis dataKey="tod" type="number" domain={[DAY_START_SEC, DAY_END_SEC]} ticks={X_TICKS_SEC} tickFormatter={formatUtcTick} label={{ value: "UTC Time", position: "insideBottom", offset: -4 }} />
                  <YAxis domain={["auto", "auto"]} allowDataOverflow label={{ value: "STEC (TECU)", angle: -90, position: "insideLeft" }} />
                  <Tooltip content={<CompactTooltip title="STEC" />} />
                  <Legend />
                  {prns.map((prn, idx) => (
                    <Line key={prn} type="monotone" dataKey={prn} name={prn} dot={false} stroke={idx % 3 === 0 ? "#0ea5e9" : "#fb7185"} strokeWidth={1} isAnimationActive={false} connectNulls />
                  ))}
                  <Line type="monotone" dataKey="median" name="median STEC" dot={false} stroke={C_BLUE} strokeWidth={3} isAnimationActive={false} />
                </LineChart>
              </ResponsiveContainer>
            </ChartBox>
          </Card>

          <Card title="Rate of TEC change index (ROTI)" subtitle="per-PRN lines (burst during disturbances)">
            <ChartBox height={350}>
              <ResponsiveContainer width="100%" height="100%" minHeight={300} minWidth={0}>
                <LineChart data={rotiSeries.length ? rotiSeries : emptyMetricSeries}>
                  <CartesianGrid stroke="#e2e8f0" />
                  <XAxis dataKey="tod" type="number" domain={[DAY_START_SEC, DAY_END_SEC]} ticks={X_TICKS_SEC} tickFormatter={formatUtcTick} label={{ value: "UTC Time", position: "insideBottom", offset: -4 }} />
                  <YAxis domain={[0, 1]} allowDataOverflow label={{ value: "ROTI (TECU/min)", angle: -90, position: "insideLeft" }} />
                  <Tooltip content={<CompactTooltip title="ROTI" />} />
                  <Legend />
                  {prns.map((prn, idx) => (
                    <Line key={prn} type="monotone" dataKey={prn} name={prn} dot={false} stroke={idx % 3 === 0 ? "#0ea5e9" : "#fb7185"} strokeWidth={1.5} opacity={0.8} isAnimationActive={false} connectNulls />
                  ))}
                  <Line type="monotone" dataKey="median" name="median ROTI" dot={false} stroke={C_ORANGE} strokeWidth={3} isAnimationActive={false} />
                </LineChart>
              </ResponsiveContainer>
            </ChartBox>
          </Card>

          <Card title="S4c (Scintillation index, corrected)">
            <ChartBox height={350}>
              <ResponsiveContainer width="100%" height="100%" minHeight={300} minWidth={0}>
                <LineChart data={s4cSeries.length ? s4cSeries : emptyMetricSeries}>
                  <CartesianGrid stroke="#e2e8f0" />
                  <XAxis dataKey="tod" type="number" domain={[DAY_START_SEC, DAY_END_SEC]} ticks={X_TICKS_SEC} tickFormatter={formatUtcTick} label={{ value: "UTC Time", position: "insideBottom", offset: -4 }} />
                  <YAxis domain={[0, 1]} allowDataOverflow />
                  <Tooltip content={<CompactTooltip title="S4c" />} />
                  <Legend />
                  {prns.map((prn, idx) => (
                    <Line key={prn} type="monotone" dataKey={prn} name={prn} dot={false} stroke={idx % 3 === 0 ? "#0ea5e9" : "#fb7185"} strokeWidth={1.5} opacity={0.9} isAnimationActive={false} connectNulls />
                  ))}
                  <Line type="monotone" dataKey="median" name="median S4c" dot={false} stroke={C_BLUE} strokeWidth={3} isAnimationActive={false} />
                </LineChart>
              </ResponsiveContainer>
            </ChartBox>
          </Card>

          <Card title="Number of satellites" subtitle="GNSS total (thick) + per-system (thin)">
            <ChartBox height={350}>
              <ResponsiveContainer width="100%" height="100%" minHeight={300} minWidth={0}>
                <LineChart data={satCount}>
                  <CartesianGrid stroke="#e2e8f0" />
                  <XAxis dataKey="tod" type="number" domain={[DAY_START_SEC, DAY_END_SEC]} ticks={X_TICKS_SEC} tickFormatter={formatUtcTick} label={{ value: "UTC Time", position: "insideBottom", offset: -4 }} />
                  <YAxis domain={[0, 40]} allowDataOverflow label={{ value: "Satellites", angle: -90, position: "insideLeft" }} />
                  <Tooltip labelFormatter={(v) => (typeof v === "number" ? formatUtcLabel(v) : String(v ?? ""))} />
                  <Legend />
                  <Line type="stepAfter" dataKey="GPS" name="GPS" dot={false} stroke="#3b82f6" strokeWidth={1.5} isAnimationActive={false} />
                  <Line type="stepAfter" dataKey="GAL" name="GAL" dot={false} stroke="#22c55e" strokeWidth={1.5} isAnimationActive={false} />
                  <Line type="stepAfter" dataKey="BDS" name="BDS" dot={false} stroke="#06b6d4" strokeWidth={1.5} isAnimationActive={false} />
                  <Line type="stepAfter" dataKey="GLO" name="GLO" dot={false} stroke="#a855f7" strokeWidth={1.5} isAnimationActive={false} />
                  <Line type="stepAfter" dataKey="QZS" name="QZS" dot={false} stroke="#eab308" strokeWidth={1.5} isAnimationActive={false} />
                  <Line type="stepAfter" dataKey="GNSS" name="GNSS" dot={false} stroke={C_GRAY} strokeWidth={3} isAnimationActive={false} />
                </LineChart>
              </ResponsiveContainer>
            </ChartBox>
          </Card>
        </div>

        <Phase2Cesium station={station} selectedDate={selectedDate} isRealtime={isRealtime} />
      </div>
    </div>
  );
}
