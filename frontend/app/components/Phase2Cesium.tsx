"use client";

import React, { useEffect, useRef, useState } from "react";

type Phase2Props = {
  station: string;
  selectedDate: string;
  isRealtime: boolean;
};

type EventPoint = {
  id: string;
  station: string;
  ts: string;
  prn: string;
  roti: number;
  lat: number;
  lon: number;
  severity: "low" | "medium" | "high";
};

type SegmentOverlay = {
  id: string;
  label: string;
  severity: "low" | "medium" | "high";
  confidence: number;
  source: string;
  polygon: Array<{ lat: number; lon: number }>;
};

type ReportItem = {
  id: string;
  title: string;
  severity: "low" | "medium" | "high";
  area: string;
  time: string;
};

type NearbyBlock = {
  enabled: boolean;
  user_lat?: number;
  user_lon?: number;
  radius_km?: number;
  prns: string[];
  events: Array<EventPoint & { distance_km?: number }>;
};

type LocationAdvisory = {
  enabled: boolean;
  in_risk_zone: boolean;
  forbidden_prns: string[];
  reason?: string;
};

type Phase2Payload = {
  ok: boolean;
  station: string;
  date: string;
  events: EventPoint[];
  segments: SegmentOverlay[];
  report: ReportItem[];
  ai?: {
    enabled?: boolean;
    active?: boolean;
    source?: string;
    cached?: boolean;
    reason?: string;
  };
  nearby: NearbyBlock;
  location_advisory?: LocationAdvisory;
};

type CesiumAny = {
  Ion: { defaultAccessToken?: string };
  Viewer: new (container: Element, options?: Record<string, unknown>) => ViewerAny;
  ArcGisMapServerImageryProvider: {
    fromBasemapType: (kind: unknown) => unknown;
    fromUrl: (url: string) => unknown;
  };
  ArcGisBaseMapType: { SATELLITE: unknown };
  SingleTileImageryProvider?: {
    fromUrl?: (url: string) => unknown;
    new (options: Record<string, unknown>): unknown;
  };
  GeoJsonDataSource: {
    load: (data: string, options?: Record<string, unknown>) => Promise<DataSourceAny>;
  };
  Color: {
    fromCssColorString: (s: string) => ColorAny;
    WHITE: ColorAny;
    BLACK: ColorAny;
    TRANSPARENT: ColorAny;
  };
  Cartesian3: {
    fromDegrees: (lon: number, lat: number, height?: number) => unknown;
    fromDegreesArray: (coords: number[]) => unknown;
    ZERO: unknown;
  };
  Math: { toDegrees: (x: number) => number };
  VerticalOrigin: { CENTER: unknown; BOTTOM: unknown };
  HeightReference: { CLAMP_TO_GROUND: unknown };
  Cartographic: {
    fromCartesian: (x: unknown) => { height: number };
  };
  Cartesian2: new (x: number, y: number) => unknown;
};

type ViewerAny = {
  entities: {
    add: (obj: Record<string, unknown>) => unknown;
    removeAll: () => void;
  };
  camera: {
    flyTo: (o: Record<string, unknown>) => void;
    position: unknown;
    moveEnd: {
      addEventListener: (fn: () => void) => void;
      removeEventListener: (fn: () => void) => void;
    };
  };
  imageryLayers: {
    addImageryProvider: (provider: unknown) => unknown;
  };
  dataSources: {
    add: (provider: Promise<DataSourceAny> | DataSourceAny) => Promise<DataSourceAny>;
  };
  scene: {
    globe: {
      baseColor: unknown;
    };
  };
  destroy: () => void;
  isDestroyed: () => boolean;
};

type DataSourceAny = {
  entities: {
    values: Array<Record<string, unknown>>;
  };
};

type ColorAny = {
  withAlpha: (v: number) => unknown;
};

declare global {
  interface Window {
    Cesium?: CesiumAny;
  }
}

const BACKEND = (process.env.NEXT_PUBLIC_BACKEND_URL ?? "http://localhost:8000")
  .trim()
  .replace(/\s+/g, "")
  .replace(/\/+$/, "");
const CESIUM_JS = "https://cesium.com/downloads/cesiumjs/releases/1.120/Build/Cesium/Cesium.js";
const CESIUM_CSS = "https://cesium.com/downloads/cesiumjs/releases/1.120/Build/Cesium/Widgets/widgets.css";
const AUTO_REFRESH_MS = 4000;
const CACHE_TTL_MS_IDLE = 2500;
const CACHE_TTL_MS_MODEL = 9000;
const NOC_ROTATE_RAD_PER_SEC = 0.0055;
const COUNTRIES_GEOJSON = "https://raw.githubusercontent.com/johan/world.geo.json/master/countries.geo.json";

function sevColor(sev: "low" | "medium" | "high") {
  if (sev === "high") return "#ef4444";
  if (sev === "medium") return "#f59e0b";
  return "#d1d5db";
}

function clamp01(v: number) {
  if (!Number.isFinite(v)) return 0;
  return Math.max(0, Math.min(1, v));
}

function mixHex(a: string, b: string, tRaw: number) {
  const t = Math.max(0, Math.min(1, tRaw));
  const pa = parseInt(a.slice(1), 16);
  const pb = parseInt(b.slice(1), 16);
  const ar = (pa >> 16) & 255;
  const ag = (pa >> 8) & 255;
  const ab = pa & 255;
  const br = (pb >> 16) & 255;
  const bg = (pb >> 8) & 255;
  const bb = pb & 255;
  const rr = Math.round(ar + (br - ar) * t);
  const rg = Math.round(ag + (bg - ag) * t);
  const rb = Math.round(ab + (bb - ab) * t);
  return `#${((1 << 24) | (rr << 16) | (rg << 8) | rb).toString(16).slice(1)}`;
}

function rotiColor01(roti: number) {
  const v = clamp01(roti);
  if (v <= 0.3) return "#d9d9d9";
  if (v <= 0.5) return mixHex("#fff200", "#ffd400", (v - 0.3) / 0.2);
  if (v <= 0.7) return mixHex("#ffd400", "#ff7a00", (v - 0.5) / 0.2);
  return mixHex("#ff7a00", "#ff0000", (v - 0.7) / 0.3);
}

function toHHMM(iso: string) {
  const d = new Date(iso);
  if (!Number.isFinite(d.getTime())) return iso;
  return d.toISOString().slice(11, 19);
}

function loadCesiumScript(): Promise<CesiumAny> {
  if (typeof window === "undefined") return Promise.reject(new Error("No window"));
  if (window.Cesium) return Promise.resolve(window.Cesium);

  return new Promise((resolve, reject) => {
    const cssId = "cesium-widgets-css";
    if (!document.getElementById(cssId)) {
      const link = document.createElement("link");
      link.id = cssId;
      link.rel = "stylesheet";
      link.href = CESIUM_CSS;
      document.head.appendChild(link);
    }

    const scriptId = "cesium-script";
    const existing = document.getElementById(scriptId) as HTMLScriptElement | null;
    if (existing) {
      existing.addEventListener("load", () => {
        if (window.Cesium) resolve(window.Cesium);
        else reject(new Error("Cesium loaded but global missing"));
      });
      existing.addEventListener("error", () => reject(new Error("Failed to load Cesium script")));
      return;
    }

    const script = document.createElement("script");
    script.id = scriptId;
    script.src = CESIUM_JS;
    script.async = true;
    script.onload = () => {
      if (window.Cesium) resolve(window.Cesium);
      else reject(new Error("Cesium loaded but global missing"));
    };
    script.onerror = () => reject(new Error("Failed to load Cesium script"));
    document.body.appendChild(script);
  });
}

export default function Phase2Cesium({ station, selectedDate, isRealtime }: Phase2Props) {
  const mapRef = useRef<HTMLDivElement | null>(null);
  const viewerRef = useRef<ViewerAny | null>(null);

  const [rotiThr, setRotiThr] = useState(0.4);
  const [radiusKm, setRadiusKm] = useState(650);
  const [userLat, setUserLat] = useState<number | null>(null);
  const [userLon, setUserLon] = useState<number | null>(null);
  const [latInput, setLatInput] = useState("");
  const [lonInput, setLonInput] = useState("");
  const [geoError, setGeoError] = useState("");

  const [events, setEvents] = useState<EventPoint[]>([]);
  const [segments, setSegments] = useState<SegmentOverlay[]>([]);
  const [report, setReport] = useState<ReportItem[]>([]);
  const [nearby, setNearby] = useState<NearbyBlock>({ enabled: false, prns: [], events: [] });
  const [aiInfo, setAiInfo] = useState<{ enabled: boolean; active: boolean; source: string; reason?: string }>({
    enabled: false,
    active: false,
    source: "heuristic",
  });
  const [advisory, setAdvisory] = useState<LocationAdvisory>({ enabled: false, in_risk_zone: false, forbidden_prns: [] });

  const queryCacheRef = useRef<Map<string, { at: number; payload: Phase2Payload }>>(new Map());
  const inFlightRef = useRef(false);

  const [cameraHeight, setCameraHeight] = useState<number | null>(null);
  const [autoRotate, setAutoRotate] = useState(false);
  const rotateTimerRef = useRef<number | null>(null);

  useEffect(() => {
    let alive = true;

    loadCesiumScript()
      .then((Cesium) => {
        if (!alive || !mapRef.current || viewerRef.current) return;

        Cesium.Ion.defaultAccessToken = "";

        const viewer = new Cesium.Viewer(mapRef.current, {
          animation: false,
          timeline: false,
          sceneModePicker: true,
          geocoder: false,
          homeButton: true,
          navigationHelpButton: false,
          selectionIndicator: false,
          infoBox: false,
          baseLayerPicker: false,
          imageryProvider: false,
        });
        viewerRef.current = viewer;
        viewer.scene.globe.baseColor = Cesium.Color.fromCssColorString("#ffffff");

        // Country boundaries and place labels over a white globe for clear monitoring view.
        try {
          const refProvider = Cesium.ArcGisMapServerImageryProvider.fromUrl(
            "https://services.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer"
          );
          viewer.imageryLayers.addImageryProvider(refProvider);
        } catch {
          // Keep base map even if reference overlay cannot be loaded.
        }

        // Local fallback overlay so country borders are still visible when remote layers are blocked.
        try {
          const SingleTile = Cesium.SingleTileImageryProvider as
            | { fromUrl?: (url: string) => unknown; new (options: Record<string, unknown>): unknown }
            | undefined;
          if (SingleTile) {
            const localProvider = SingleTile.fromUrl
              ? SingleTile.fromUrl("/world_map.png")
              : new SingleTile({ url: "/world_map.png" });
            const localLayer = viewer.imageryLayers.addImageryProvider(localProvider) as { alpha?: number };
            localLayer.alpha = 0.5;
          }
        } catch {
          // Optional fallback only; continue rendering even if the local overlay fails.
        }

        try {
          void viewer.dataSources
            .add(
              Cesium.GeoJsonDataSource.load(COUNTRIES_GEOJSON, {
                stroke: Cesium.Color.BLACK,
                strokeWidth: 1.2,
                fill: Cesium.Color.TRANSPARENT,
                clampToGround: true,
              })
            )
            .then((dataSource) => {
              for (const entity of dataSource.entities.values) {
                const polygon = entity.polygon as { outline?: boolean; outlineColor?: unknown; material?: unknown } | undefined;
                if (polygon) {
                  polygon.outline = true;
                  polygon.outlineColor = Cesium.Color.BLACK;
                  polygon.material = Cesium.Color.TRANSPARENT;
                }
                const polyline = entity.polyline as { material?: unknown; width?: number } | undefined;
                if (polyline) {
                  polyline.material = Cesium.Color.BLACK;
                  polyline.width = 1.2;
                }
              }
            })
            .catch(() => {
              // Boundary overlay is optional; keep the viewer usable if remote GeoJSON fails.
            });
        } catch {
          // Boundary overlay is optional; keep the viewer usable if remote GeoJSON fails.
        }

        const updateHeight = () => {
          const cart = Cesium.Cartographic.fromCartesian(viewer.camera.position);
          setCameraHeight(Number.isFinite(cart.height) ? cart.height : null);
        };
        updateHeight();
        viewer.camera.moveEnd.addEventListener(updateHeight);

      })
      .catch((err) => {
        console.error(err);
      });

    return () => {
      alive = false;
      if (rotateTimerRef.current !== null) {
        window.clearInterval(rotateTimerRef.current);
        rotateTimerRef.current = null;
      }
      const viewer = viewerRef.current;
      viewerRef.current = null;
      if (viewer && !viewer.isDestroyed()) viewer.destroy();
    };
  }, []);

  useEffect(() => {
    const viewer = viewerRef.current;
    const Cesium = window.Cesium;
    if (!viewer || !Cesium) return;

    if (rotateTimerRef.current !== null) {
      window.clearInterval(rotateTimerRef.current);
      rotateTimerRef.current = null;
    }
    if (!autoRotate) return;

    const rotateAxis = (Cesium.Cartesian3 as unknown as { UNIT_Z?: unknown }).UNIT_Z ?? Cesium.Cartesian3.ZERO;
    let last = performance.now();
    rotateTimerRef.current = window.setInterval(() => {
      try {
        const now = performance.now();
        const dt = Math.max(0, (now - last) / 1000);
        last = now;
        const cam = viewer.camera as unknown as { rotate?: (axis: unknown, amount: number) => void };
        if (cam.rotate) cam.rotate(rotateAxis, -NOC_ROTATE_RAD_PER_SEC * dt);
      } catch {
        // Ignore rotation glitches while viewer is reconfiguring.
      }
    }, 66);

    return () => {
      if (rotateTimerRef.current !== null) {
        window.clearInterval(rotateTimerRef.current);
        rotateTimerRef.current = null;
      }
    };
  }, [autoRotate]);

  useEffect(() => {
    let alive = true;

    const poll = () => {
      if (inFlightRef.current) return;
      const q = new URLSearchParams({
        station,
        date: selectedDate,
        roti_thr: rotiThr.toString(),
        user_radius_km: radiusKm.toString(),
        enable_ai: "1",
        all_stations: "1",
        max_events: "2400",
      });
      if (userLat !== null && userLon !== null) {
        q.set("user_lat", userLat.toString());
        q.set("user_lon", userLon.toString());
      }

      const url = `${BACKEND}/api/phase2/events?${q.toString()}`;
      const cacheTtl = userLat !== null && userLon !== null ? CACHE_TTL_MS_MODEL : CACHE_TTL_MS_IDLE;
      const cacheHit = queryCacheRef.current.get(url);
      const now = Date.now();
      if (cacheHit && now - cacheHit.at <= cacheTtl) {
        const j = cacheHit.payload;
        setEvents(Array.isArray(j.events) ? j.events : []);
        setSegments(Array.isArray(j.segments) ? j.segments : []);
        setReport(Array.isArray(j.report) ? j.report : []);
        setNearby(j.nearby && Array.isArray(j.nearby.prns) ? j.nearby : { enabled: false, prns: [], events: [] });
        setAdvisory(j.location_advisory ?? { enabled: false, in_risk_zone: false, forbidden_prns: [] });
        setAiInfo({
          enabled: !!j.ai?.enabled,
          active: !!j.ai?.active,
          source: j.ai?.source || "heuristic",
          reason: j.ai?.reason,
        });
        return;
      }

      inFlightRef.current = true;
      const controller = new AbortController();
      const abortTimer = window.setTimeout(() => controller.abort(), 7000);

      fetch(url, { signal: controller.signal })
        .then((r) => r.json())
        .then((j: Phase2Payload) => {
          if (!alive || !j?.ok) return;
          queryCacheRef.current.set(url, { at: Date.now(), payload: j });
          if (queryCacheRef.current.size > 25) {
            const firstKey = queryCacheRef.current.keys().next().value;
            if (firstKey) queryCacheRef.current.delete(firstKey);
          }
          setEvents(Array.isArray(j.events) ? j.events : []);
          setSegments(Array.isArray(j.segments) ? j.segments : []);
          setReport(Array.isArray(j.report) ? j.report : []);
          setNearby(j.nearby && Array.isArray(j.nearby.prns) ? j.nearby : { enabled: false, prns: [], events: [] });
          setAdvisory(j.location_advisory ?? { enabled: false, in_risk_zone: false, forbidden_prns: [] });
          setAiInfo({
            enabled: !!j.ai?.enabled,
            active: !!j.ai?.active,
            source: j.ai?.source || "heuristic",
            reason: j.ai?.reason,
          });
        })
        .catch(() => {
          if (!alive) return;
          setEvents([]);
          setSegments([]);
          setReport([]);
          setNearby({ enabled: false, prns: [], events: [] });
          setAdvisory({ enabled: false, in_risk_zone: false, forbidden_prns: [] });
          setAiInfo({ enabled: false, active: false, source: "offline" });
        })
        .finally(() => {
          window.clearTimeout(abortTimer);
          inFlightRef.current = false;
        });
    };

    poll();
    if (!isRealtime) {
      return () => {
        alive = false;
      };
    }

    const timer = window.setInterval(poll, AUTO_REFRESH_MS);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, [station, selectedDate, isRealtime, rotiThr, userLat, userLon, radiusKm]);

  useEffect(() => {
    const viewer = viewerRef.current;
    const Cesium = window.Cesium;
    if (!viewer || !Cesium) return;

    viewer.entities.removeAll();

    for (const e of events) {
      const color = Cesium.Color.fromCssColorString(rotiColor01(e.roti));
      viewer.entities.add({
        id: e.id,
        position: Cesium.Cartesian3.fromDegrees(e.lon, e.lat, 250000),
        point: {
          pixelSize: 7,
          color,
          outlineColor: Cesium.Color.WHITE,
          outlineWidth: 1,
          heightReference: Cesium.HeightReference.CLAMP_TO_GROUND,
        },
      });
    }

    for (const seg of segments) {
      const polyCoords: number[] = [];
      for (const p of seg.polygon) {
        polyCoords.push(p.lon, p.lat);
      }
      const fill = Cesium.Color.fromCssColorString(sevColor(seg.severity)).withAlpha(0.22);

      viewer.entities.add({
        id: seg.id,
        polygon: {
          hierarchy: Cesium.Cartesian3.fromDegreesArray(polyCoords),
          material: fill,
          outline: true,
          outlineColor: Cesium.Color.fromCssColorString(sevColor(seg.severity)),
        },
      });
    }

    if (userLat !== null && userLon !== null) {
      viewer.entities.add({
        id: "user-marker",
        position: Cesium.Cartesian3.fromDegrees(userLon, userLat, 0),
        point: {
          pixelSize: 11,
          color: Cesium.Color.WHITE,
          outlineColor: Cesium.Color.fromCssColorString("#0ea5e9"),
          outlineWidth: 3,
          heightReference: Cesium.HeightReference.CLAMP_TO_GROUND,
        },
      });
      viewer.camera.flyTo({
        destination: Cesium.Cartesian3.fromDegrees(userLon, userLat, 1750000),
        duration: 0.9,
      });
    }
  }, [events, segments, userLat, userLon]);

  const useDeviceLocation = () => {
    if (!("geolocation" in navigator)) {
      setGeoError("Browser does not support geolocation.");
      return;
    }
    setGeoError("");
    navigator.geolocation.getCurrentPosition(
      (p) => {
        const lat = Number(p.coords.latitude.toFixed(6));
        const lon = Number(p.coords.longitude.toFixed(6));
        setUserLat(lat);
        setUserLon(lon);
        setLatInput(String(lat));
        setLonInput(String(lon));
      },
      (err) => {
        setGeoError(err.message || "Location permission denied.");
      },
      { enableHighAccuracy: true, timeout: 12000 }
    );
  };

  const applyManualLocation = () => {
    const lat = Number(latInput);
    const lon = Number(lonInput);
    if (!Number.isFinite(lat) || !Number.isFinite(lon)) {
      setGeoError("Invalid location values. Use decimal lat/lon.");
      return;
    }
    if (lat < -90 || lat > 90 || lon < -180 || lon > 180) {
      setGeoError("Lat must be -90..90 and lon must be -180..180.");
      return;
    }
    setGeoError("");
    setUserLat(Number(lat.toFixed(6)));
    setUserLon(Number(lon.toFixed(6)));
  };

  return (
    <section className="bg-white border border-slate-200 rounded-2xl p-4 shadow-sm">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className="font-semibold">Next (Phase 2)</div>
          <div className="text-slate-600 mt-1">Cesium IPP globe + AI segmentation overlay + location safety advisory</div>
          <div className="text-xs text-slate-500 mt-1">IPP dots: all-station realtime values with ROTI gradient 0.0 to 1.0</div>
        </div>
        <div className="text-xs rounded-full px-2 py-1 bg-slate-100 text-slate-600">
          AI overlay: ON (always)
          <div className="text-xs text-slate-500 mt-1">All-stations mode locked ON</div>
        </div>
      </div>

      <div className="mt-4 grid grid-cols-1 xl:grid-cols-[2fr_1fr] gap-4">
        <div className="space-y-3">
          <div ref={mapRef} className="h-[440px] w-full overflow-hidden rounded-xl border border-slate-200 bg-white" />
          <div className="grid grid-cols-1 md:grid-cols-4 gap-3 text-sm">
            <label className="flex flex-col gap-1">
              <span className="text-slate-600">ROTI threshold</span>
              <input
                type="number"
                min={0.01}
                max={2}
                step={0.05}
                className="bg-white border border-slate-300 rounded-lg px-2 py-1"
                value={rotiThr}
                onChange={(e) => setRotiThr(Number(e.target.value) || 0.4)}
              />
            </label>
            <label className="flex flex-col gap-1">
              <span className="text-slate-600">User zone radius (km)</span>
              <input
                type="number"
                min={50}
                max={2000}
                step={25}
                className="bg-white border border-slate-300 rounded-lg px-2 py-1"
                value={radiusKm}
                onChange={(e) => setRadiusKm(Number(e.target.value) || 650)}
              />
            </label>
            <label className="flex flex-col gap-1">
              <span className="text-slate-600">Lat</span>
              <input
                type="text"
                inputMode="decimal"
                className="bg-white border border-slate-300 rounded-lg px-2 py-1"
                value={latInput}
                onChange={(e) => setLatInput(e.target.value)}
                placeholder="13.7563"
              />
            </label>
            <label className="flex flex-col gap-1">
              <span className="text-slate-600">Lon</span>
              <input
                type="text"
                inputMode="decimal"
                className="bg-white border border-slate-300 rounded-lg px-2 py-1"
                value={lonInput}
                onChange={(e) => setLonInput(e.target.value)}
                placeholder="100.5018"
              />
            </label>
          </div>
          <div className="flex flex-wrap gap-2 items-center">
            <button
              type="button"
              onClick={applyManualLocation}
              className="rounded-lg border border-slate-300 bg-white px-3 py-1.5 text-sm hover:bg-slate-50"
            >
              Apply location
            </button>
            <button
              type="button"
              onClick={useDeviceLocation}
              className="rounded-lg border border-slate-300 bg-white px-3 py-1.5 text-sm hover:bg-slate-50"
            >
              Use device location
            </button>
            <button
              type="button"
              onClick={() => setAutoRotate((v) => !v)}
              className="rounded-lg border border-slate-300 bg-white px-3 py-1.5 text-sm hover:bg-slate-50"
            >
              {autoRotate ? "Stop globe rotation" : "Start globe rotation"}
            </button>
            {geoError ? <span className="text-xs text-amber-700">{geoError} (IPP plotting still works)</span> : null}
            <span className="text-xs text-slate-500">
              Cesium camera height: {cameraHeight === null ? "-" : `${Math.round(cameraHeight).toLocaleString()} m`}
            </span>
            <span className="text-xs text-slate-500">Auto-rotate: {autoRotate ? "ON" : "OFF"}</span>
          </div>
        </div>

        <div className="border border-slate-200 rounded-xl p-4 bg-slate-50 space-y-4">
          <div className="rounded-lg border border-slate-200 bg-white p-2">
            <div className="text-xs text-slate-600 mb-2">ROTI color scale (0 to 1)</div>
            <div
              className="h-3 w-full rounded"
              style={{
                background:
                  "linear-gradient(to right, #d9d9d9 0%, #d9d9d9 30%, #fff200 50%, #ff7a00 70%, #ff0000 100%)",
              }}
            />
            <div className="mt-1 flex justify-between text-[10px] text-slate-500">
              <span>0.0</span>
              <span>0.3</span>
              <span>0.5</span>
              <span>0.7</span>
              <span>1.0</span>
            </div>
          </div>

          <div>
            <div className="font-semibold text-slate-900">Alert report</div>
            <div className="text-xs text-slate-500 mt-1">Thresholded ROTI + segmentation overlay metadata</div>
          </div>

          <div className="space-y-2 max-h-[250px] overflow-auto pr-1">
            {report.length ? (
              report.map((a) => (
                <div key={a.id} className="rounded-lg bg-white border border-slate-200 p-2">
                  <div className="flex items-center justify-between gap-2">
                    <div className="font-medium text-sm text-slate-900">{a.title}</div>
                    <span className="text-[10px] px-2 py-0.5 rounded-full text-white" style={{ backgroundColor: sevColor(a.severity) }}>
                      {a.severity.toUpperCase()}
                    </span>
                  </div>
                  <div className="text-xs text-slate-500 mt-1">{a.area}</div>
                  <div className="text-xs text-slate-400">{toHHMM(a.time)}</div>
                </div>
              ))
            ) : (
              <div className="text-sm text-slate-500">No events above threshold.</div>
            )}
          </div>

          <div>
            <div className="font-semibold text-slate-900">PRNs over user zone</div>
            {!nearby.enabled ? (
              <div className="text-sm text-slate-500 mt-1">Set location or allow device position to detect passing PRNs.</div>
            ) : nearby.prns.length ? (
              <div className="mt-2 flex flex-wrap gap-2">
                {nearby.prns.map((prn) => (
                  <span key={prn} className="text-xs rounded-full px-2 py-1 bg-blue-100 text-blue-800 border border-blue-200">
                    {prn}
                  </span>
                ))}
              </div>
            ) : (
              <div className="text-sm text-slate-500 mt-1">No PRN hotspots in the selected user zone.</div>
            )}
          </div>

          <div>
            <div className="font-semibold text-slate-900">Satellite usage advisory</div>
            {!advisory.enabled ? (
              <div className="text-sm text-slate-500 mt-1">Set location to receive satellite safety advisory.</div>
            ) : advisory.in_risk_zone ? (
              <div className="mt-2 space-y-2">
                <div className="text-sm text-rose-700 font-medium">Current location is inside segmented risk zone.</div>
                <div className="text-xs text-slate-600">Avoid PRNs:</div>
                <div className="flex flex-wrap gap-2">
                  {advisory.forbidden_prns.length ? (
                    advisory.forbidden_prns.map((prn) => (
                      <span key={prn} className="text-xs rounded-full px-2 py-1 bg-rose-100 text-rose-800 border border-rose-200">
                        {prn}
                      </span>
                    ))
                  ) : (
                    <span className="text-xs text-slate-500">No specific PRN flagged.</span>
                  )}
                </div>
              </div>
            ) : (
              <div className="text-sm text-emerald-700 mt-1">No satellite restriction for current location.</div>
            )}
          </div>

          <div className="text-xs text-slate-500">
            Events plotted: <b>{events.length}</b> | Segments: <b>{segments.length}</b>
          </div>
          <div className="text-xs text-slate-500">
            AI source: <b>{aiInfo.source}</b>
            {aiInfo.reason ? ` (${aiInfo.reason})` : ""}
          </div>
          <div className="text-xs text-slate-500">AI input domain: white-canvas hotspot image</div>
        </div>
      </div>
    </section>
  );
}
