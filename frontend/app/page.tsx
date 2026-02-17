"use client";

import React, { useEffect, useMemo, useRef, useState } from "react";
import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  Tooltip,
  Legend,
  ResponsiveContainer,
  CartesianGrid,
} from "recharts";

type Station = { id: string; name: string; lat?: number; lon?: number; alt?: number };

// realtime sample ที่ backend ส่งมา
type Sample = { ts: string; prn: string; S4c: number | null; ROTI: number | null; VTEC: number | null };

// sat count per time
type CountPoint = {
  ts: string;
  time: string;
  GNSS: number;
  GPS?: number;
  GAL?: number;
  BDS?: number;
  GLO?: number;
  QZS?: number;
  SBAS?: number;
  IRNSS?: number;
};

const BACKEND = "http://localhost:8000";

// --- UI colors (light + orange/blue) ---
const C_BLUE = "#2563eb"; // blue-600
const C_ORANGE = "#f97316"; // orange-500
const C_GRAY = "#64748b"; // slate-500

type MetricKey = "VTEC" | "ROTI" | "S4c";
type MetricRow = {
  ts: string;
  time: string;
  median: number | null;
  [key: string]: number | string | null;
};

function toHHMMSS(iso: string) {
  try {
    const d = new Date(iso);
    return d.toISOString().slice(11, 19);
  } catch {
    return iso;
  }
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

function toNum(v: unknown): number | null {
  const n = typeof v === "number" ? v : Number(v);
  return Number.isFinite(n) ? n : null;
}

function normalizeSample(s: Sample): Sample {
  return {
    ts: s.ts,
    prn: s.prn,
    S4c: toNum(s.S4c),
    ROTI: toNum(s.ROTI),
    VTEC: toNum(s.VTEC),
  };
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
      const v = s[metric];
      map[s.ts] = v ?? null;
    }
    perPrn[prn] = map;
  }

  const tsList = Array.from(tsSet).sort();
  return tsList.map((ts) => {
    const row: MetricRow = { ts, time: toHHMMSS(ts), median: null };
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
  payload?: any[];
  label?: string;
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
      <div className="text-slate-500">{label}</div>
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

export default function Page() {
  const [stations, setStations] = useState<Station[]>([]);
  const [station, setStation] = useState("KMIT6");

  const today = new Date().toISOString().slice(0, 10);
  const [selectedDate, setSelectedDate] = useState(today);
  const isRealtime = selectedDate === today;

  // per-PRN time series
  const [series, setSeries] = useState<Record<string, Sample[]>>({});
  const wsRef = useRef<WebSocket | null>(null);
  const realtimeWindowMs = 24 * 60 * 60 * 1000;

  // load stations
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
        setStation((prev) => (prev === "KMIT6" ? prev : "KMIT6"));
      });
    return () => {
      alive = false;
    };
  }, []);

  // reset when station/date changes
  useEffect(() => {
    setSeries({});
  }, [station, selectedDate]);

  // REALTIME websocket
  useEffect(() => {
    if (!isRealtime) return;

    if (wsRef.current) wsRef.current.close();

    const ws = new WebSocket(`ws://localhost:8000/ws/realtime?station=${encodeURIComponent(station)}`);
    wsRef.current = ws;

    ws.onopen = () => ws.send(JSON.stringify({ station }));

    ws.onmessage = (ev) => {
      let msg: any = null;
      try {
        msg = JSON.parse(ev.data);
      } catch {
        return;
      }
      if (msg.station && String(msg.station).toUpperCase() !== station.toUpperCase()) return;
      const dataRaw: Sample[] = Array.isArray(msg.data) ? msg.data : [];
      const data = dataRaw.map(normalizeSample).filter((s) => s.ts && s.prn);
      if (!data.length) return;

      setSeries((prev) => {
        const next = { ...prev };
        for (const s of data) {
          const prn = s.prn;
          const arr = next[prn] ? [...next[prn]] : [];
          arr.push(s);
          const latestMs = Date.parse(s.ts);
          if (Number.isFinite(latestMs)) {
            const cutoff = latestMs - realtimeWindowMs;
            while (arr.length && Date.parse(arr[0].ts) < cutoff) {
              arr.shift();
            }
          }
          next[prn] = arr;
        }
        return next;
      });
    };

    ws.onerror = () => {
      // ปล่อยให้ console แสดง error ได้ตามปกติ
    };

    return () => ws.close();
  }, [isRealtime, station]);

  // POST: load CSV history
  useEffect(() => {
    if (isRealtime) return;

    fetch(
      `${BACKEND}/api/history?station=${encodeURIComponent(station)}&date=${encodeURIComponent(
        selectedDate
      )}`
    )
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
  const rotiSeries = useMemo(() => buildMetricSeries("ROTI", prns, series), [series, prns]);
  const s4cSeries = useMemo(() => buildMetricSeries("S4c", prns, series), [series, prns]);

  const emptyMetricSeries = useMemo(
    () => [{ ts: new Date().toISOString(), time: toHHMMSS(new Date().toISOString()), median: null }],
    []
  );

  // ----- Build sat counts per time -----
  const satCount = useMemo<CountPoint[]>(() => {
    const prnByTs: Record<string, Set<string>> = {};
    for (const prn of prns) {
      for (const s of series[prn] ?? []) {
        (prnByTs[s.ts] ??= new Set()).add(prn);
      }
    }
    const tsList = Object.keys(prnByTs).sort();
    return tsList.map((ts) => {
      const set = prnByTs[ts];
      const counts: any = { ts, time: toHHMMSS(ts) };
      counts.GNSS = set.size;

      const bySys: Record<string, number> = {};
      for (const p of set) {
      const sys = sysOfPRN(p);
      if (!sys) continue; 
      bySys[sys] = (bySys[sys] ?? 0) + 1;
      }
      counts.GPS = bySys["GPS"] ?? 0;
      counts.GAL = bySys["GAL"] ?? 0;
      counts.BDS = bySys["BDS"] ?? 0;
      counts.GLO = bySys["GLO"] ?? 0;
      counts.QZS = bySys["QZS"] ?? 0;
      counts.SBAS = bySys["SBAS"] ?? 0;
      counts.IRNSS = bySys["IRNSS"] ?? 0;

      return counts as CountPoint;
    });
  }, [series, prns]);

  return (
    <div className="min-h-screen bg-white text-slate-900">
      <div className="max-w-6xl mx-auto p-6 space-y-6">
        {/* Header */}
        <header className="space-y-1">
          <h1 className="text-2xl font-semibold">
            <span className="text-slate-900">GNSS</span>{" "}
            <span className="text-blue-600">Scintillation</span>{" "}
            <span className="text-orange-500">&</span>{" "}
            <span className="text-blue-600">Ionosphere</span> Monitor
          </h1>
          <p className="text-slate-600">
            Realtime / Post-process plots: <b>S4c</b>, <b>ROTI</b>, <b>TEC(VTEC)</b>, <b>Satellite count</b>
          </p>
        </header>

        {/* Controls */}
        <section className="bg-white border border-slate-200 rounded-2xl p-4 flex flex-wrap gap-4 items-end shadow-sm">
          <div className="flex flex-col gap-1">
            <label className="text-sm text-slate-600">Station</label>
            <select
              className="bg-white border border-slate-300 rounded-xl px-3 py-2 focus:outline-none focus:ring-2 focus:ring-blue-200"
              value={station}
              onChange={(e) => setStation(e.target.value)}
            >
              {stations.map((s) => (
                <option key={s.id} value={s.id}>
                  {s.id} - {s.name}
                </option>
              ))}
            </select>
          </div>

          <div className="flex flex-col gap-1">
            <label className="text-sm text-slate-600">Date</label>
            <input
              className="bg-white border border-slate-300 rounded-xl px-3 py-2 focus:outline-none focus:ring-2 focus:ring-blue-200"
              type="date"
              value={selectedDate}
              onChange={(e) => setSelectedDate(e.target.value)}
            />
            <div className="text-xs text-slate-500">{isRealtime ? "Realtime mode (Today)" : "Post-process mode (CSV)"}</div>
          </div>

          <div className="ml-auto text-sm text-slate-600">
            Active PRNs: <span className="text-slate-900 font-semibold">{prns.length}</span>
          </div>
        </section>

        {/* 4 panels */}
        <div className="space-y-4">
          {/* 1) TEC/VTEC */}
          <Card title="GNSS TEC / VTEC (per-PRN + median)" subtitle="เส้นบาง = per-PRN, เส้นหนา = median">
            <div style={{ width: "100%", minWidth: 0 }}>
              <ResponsiveContainer width="100%" height={350}>
                {/* ✅ ใส่ data ให้ LineChart */}
                <LineChart data={vtecSeries.length ? vtecSeries : emptyMetricSeries}>
                  <CartesianGrid stroke="#e2e8f0" />
                  <XAxis dataKey="time" type="category" allowDuplicatedCategory={false} />
                  <YAxis
                    domain={[0, 100]}
                    allowDataOverflow
                    label={{ value: "TEC (TECU)", angle: -90, position: "insideLeft" }}
                  />
                  <Tooltip content={<CompactTooltip title="VTEC" />} />
                  <Legend />

                  {/* per-PRN */}
                  {prns.map((prn, idx) => (
                    <Line
                      key={prn}
                      type="monotone"
                      dataKey={prn}
                      name={prn}
                      dot={false}
                      stroke={idx % 3 === 0 ? "#0ea5e9" : "#fb7185"}
                      strokeWidth={1}
                      isAnimationActive={false}
                      connectNulls={false}
                    />
                  ))}

                  {/* median */}
                  <Line
                    type="monotone"
                    dataKey="median"
                    name="median VTEC"
                    dot={false}
                    stroke={C_BLUE}
                    strokeWidth={3}
                    isAnimationActive={false}
                  />
                </LineChart>
              </ResponsiveContainer>
            </div>
          </Card>

          {/* 2) ROTI */}
          <Card title="Rate of TEC change index (ROTI)" subtitle="เส้น per-PRN (ดู burst ช่วง disturb)">
            <div style={{ width: "100%", minWidth: 0 }}>
              <ResponsiveContainer width="100%" height={350}>
                {/* ✅ ใส่ data ให้ LineChart */}
                <LineChart data={rotiSeries.length ? rotiSeries : emptyMetricSeries}>
                  <CartesianGrid stroke="#e2e8f0" />
                  <XAxis dataKey="time" type="category" allowDuplicatedCategory={false} />
                  <YAxis
                    domain={[0, 1]}
                    allowDataOverflow
                    label={{ value: "ROTI (TECU/min)", angle: -90, position: "insideLeft" }}
                  />
                  <Tooltip content={<CompactTooltip title="ROTI" />} />
                  <Legend />

                  {prns.map((prn, idx) => (
                    <Line
                      key={prn}
                      type="monotone"
                      dataKey={prn}
                      name={prn}
                      dot={false}
                      stroke={idx % 3 === 0 ? "#0ea5e9" : "#fb7185"}
                      strokeWidth={1.5}
                      opacity={0.8}
                      isAnimationActive={false}
                      connectNulls={false}
                    />
                  ))}

                  <Line
                    type="monotone"
                    dataKey="median"
                    name="median ROTI"
                    dot={false}
                    stroke={C_ORANGE}
                    strokeWidth={3}
                    isAnimationActive={false}
                  />
                </LineChart>
              </ResponsiveContainer>
            </div>
          </Card>

          {/* 3) S4c */}
          <Card title="S4c (Scintillation index, corrected)" subtitle="เพิ่มช่องนี้จากรูปเดิม">
            <div style={{ width: "100%", minWidth: 0 }}>
              <ResponsiveContainer width="100%" height={350}>
                {/* ✅ ใส่ data ให้ LineChart */}
                <LineChart data={s4cSeries.length ? s4cSeries : emptyMetricSeries}>
                  <CartesianGrid stroke="#e2e8f0" />
                  <XAxis dataKey="time" type="category" allowDuplicatedCategory={false} />
                  <YAxis domain={[0, 1]} allowDataOverflow />
                  <Tooltip content={<CompactTooltip title="S4c" />} />
                  <Legend />

                  {prns.map((prn, idx) => (
                    <Line
                      key={prn}
                      type="monotone"
                      dataKey={prn}
                      name={prn}
                      dot={false}
                      stroke={idx % 3 === 0 ? "#0ea5e9" : "#fb7185"}
                      strokeWidth={1.5}
                      opacity={0.9}
                      isAnimationActive={false}
                      connectNulls={false}
                    />
                  ))}

                  <Line
                    type="monotone"
                    dataKey="median"
                    name="median S4c"
                    dot={false}
                    stroke={C_BLUE}
                    strokeWidth={3}
                    isAnimationActive={false}
                  />
                </LineChart>
              </ResponsiveContainer>
            </div>
          </Card>

          {/* 4) Number of satellites */}
          <Card title="Number of satellites" subtitle="รวม GNSS (เส้นหนา) + แยกระบบ (เส้นบาง)">
            <div style={{ width: "100%", minWidth: 0 }}>
              <ResponsiveContainer width="100%" height={350}>
                <LineChart data={satCount}>
                  <CartesianGrid stroke="#e2e8f0" />
                  <XAxis dataKey="time" />
                  <YAxis
                    domain={[0, 40]}
                    allowDataOverflow
                    label={{ value: "Satellites", angle: -90, position: "insideLeft" }}
                  />
                  <Tooltip />
                  <Legend />
                  <Line type="stepAfter" dataKey="GPS" name="GPS" dot={false} stroke="#3b82f6" strokeWidth={1.5} isAnimationActive={false} />
                  <Line type="stepAfter" dataKey="GAL" name="GAL" dot={false} stroke="#22c55e" strokeWidth={1.5} isAnimationActive={false} />
                  <Line type="stepAfter" dataKey="BDS" name="BDS" dot={false} stroke="#06b6d4" strokeWidth={1.5} isAnimationActive={false} />
                  <Line type="stepAfter" dataKey="GLO" name="GLO" dot={false} stroke="#a855f7" strokeWidth={1.5} isAnimationActive={false} />
                  <Line type="stepAfter" dataKey="QZS" name="QZS" dot={false} stroke="#eab308" strokeWidth={1.5} isAnimationActive={false} />
                  <Line type="stepAfter" dataKey="GNSS" name="GNSS" dot={false} stroke={C_GRAY} strokeWidth={3} isAnimationActive={false} />
                </LineChart>
              </ResponsiveContainer>
            </div>
          </Card>
        </div>

        <section className="bg-white border border-slate-200 rounded-2xl p-4 shadow-sm">
          <div className="font-semibold">ถัดไป (Phase 2)</div>
          <div className="text-slate-600 mt-1">เพิ่ม IPP map + AI segmentation overlay + Alert report</div>
        </section>
      </div>
    </div>
  );
}

