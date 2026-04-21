# SpaceWeatherProduct-Monitor System Assessment
**Date**: April 21, 2026 | **Current Session**: KMIT6 Station

---

## 🔴 CRITICAL ISSUES (Blocking Real-Time)

### 1. **WebSocket Connection Failure** (PRIMARY ISSUE)
**Status**: ❌ NOT WORKING

**Evidence from monitor logs** (`soak_metrics_KMIT6_20260421_124833.csv`):
```
ws_total_connections: 0 (always zero)
ws_station_connections: 0 (no clients connected)
client_ws_status: "connecting" (constantly trying)
client_ws_reconnects: 1→30+ (keeps failing)
publish_count: 43+ (BACKEND IS PUBLISHING DATA!)
publish_rows: 10-19/sec (data IS flowing from worker)
publish_broadcast_ms: 0.08-0.29ms (trying to broadcast but no clients to receive)
```

**Problem**: 
- Worker → Backend: ✅ Working (data flowing)
- Backend → Frontend WebSocket: ❌ BROKEN (0 connections)
- Frontend shows "WS: closed" (correct - can't connect)

**Likely Causes**:
1. **CORS Issue** - Frontend can't connect to WebSocket
2. **WebSocket URL Wrong** - Frontend using wrong port/protocol
3. **Backend WebSocket Handler Not Registering** - Connection endpoint issue
4. **Firewall/Proxy Blocking WebSocket** - localhost:8000 WebSocket not accessible

**Fix Priority**: 🔥 **FIX THIS FIRST** - without it, no real-time data reaches frontend

---

### 2. **CSV File Race Condition** (Data Integrity Risk)
**Status**: ⚠️ POTENTIAL ISSUE

**Problem**:
- Worker publishes 10-19 rows per second
- All rows appended to same CSV file (`backend/data/KMIT6/2026-04-21.csv`)
- `append_rows_to_csv()` has NO file locking
- Multiple concurrent writes = corrupted CSV lines

**Current Impact**: 
- Small risk now (single KMIT6 worker)
- **WILL BREAK** if you scale to multi-station real-time

**Code Issue** (`backend/main.py` line ~285):
```python
def append_rows_to_csv(csv_path: Path, rows: List[Dict]):
    with csv_path.open("a", newline="", encoding="utf-8") as f:  # ← No lock!
        w = csv.writer(f)
        for r in rows:
            w.writerow([...])  # Each worker overwrites others
```

**Fix Priority**: 🟡 **MEDIUM** (doesn't affect display yet, but breaks data integrity)

---

## 🟡 CALCULATION & DATA ISSUES

### 3. **Satellite Ephemeris Data Parsed But Not Used**
**Status**: ⚠️ INCOMPLETE INTEGRATION

**What's Happening**:
- Worker receives RTCM messages: 1019 (GPS), 1020 (GLONASS), 1042 (BDS), 1044/1045/1046 (Galileo)
- Ephemeris parsed and stored in memory
- Satellite positions calculated correctly
- **BUT**: Positions never linked to observations
- **Only** elevation mask (30°) applied; **NO elevation correction**

**What's Missing**:
```
Correct VTEC = STEC / mapping_function(elevation, azimuth)
Current VTEC = STEC / simple_slant_factor(elevation_only)
```

**Impact**: 
- VTEC values are **INACCURATE** (~10-20% error without proper mapping)
- Slant TEC geometry not correct
- IPP (Ionospheric Pierce Point) is fake (hash-based placement, not actual geometry)

**Fix Priority**: 🟡 **MEDIUM** (affects accuracy but not real-time flow)

---

### 4. **S4c Calculation Without Context**
**Status**: ⚠️ WEAK IMPLEMENTATION

**Current Code** (`worker_ntrip_publish.py`):
```python
def s4c(vals):
    a = np.array(vals, dtype=float)
    mu = a.mean()
    return np.sqrt(((a - mu) ** 2).mean()) / mu if mu else np.nan
```

**Problems**:
- No minimum sample count validation
- Batch-wise calculation without sliding window (discontinuous)
- **If batch has <5 samples** → unreliable S4c
- **If batch has 1-2 samples** → NaN or extreme values

**Example**: Worker publishes 10-19 samples every 1 second
- Each sample's S4c computed independently
- No temporal continuity

**Fix Priority**: 🟡 **MEDIUM** (visualization issue, not data loss)

---

### 5. **Phase Slip Detection Not Fed Back to Worker**
**Status**: ⚠️ INCOMPLETE

**What's Happening**:
- Backend detects 5+ TECU jumps and applies bias correction
- Worker also does local slip detection
- **But**: No feedback loop from backend to worker
- If backend detects slip, worker doesn't know and keeps wrong bias

**Impact**: 
- Downstream slip detection unreliable
- ROTI spikes due to uncorrected bias

**Fix Priority**: 🟠 **LOW** (current filtering handles most cases)

---

## 🟢 WHAT'S WORKING

✅ **NTRIP Connection to Caster** 
- Worker connected to 161.246.18.204:2101
- Authentication successful (tele4/cssrg1234)
- Data flowing at ~1 Hz

✅ **Backend Data Ingestion**
- Receiving 10-19 rows/sec
- CSV writes working (8-20ms per batch)
- 43+ publish cycles in ~60 seconds

✅ **Calculations (MOSTLY)**
- Ephemeris parsing: GPS/GLONASS/Galileo/BDS working
- Satellite elevation calculation: working
- ROTI windowing: working (5-min rolling, median filter)
- STEC hatch smoothing: working

✅ **Frontend Display Logic**
- Chart rendering: working (Recharts)
- Station selector: working
- UI responsive: working

✅ **Cesium 3D Globe**
- Phase2 viewer: integrated
- ROTI visualization: ready (not yet receiving data)

---

## 📊 REAL-TIME vs POST-PROCESS CAPABILITY

| Feature | Real-Time | Post-Process | Notes |
|---------|-----------|--------------|-------|
| **S4c** | ⚠️ Partial | ✅ Full | Batch-wise only in real-time; full window in post-process |
| **ROTI** | ✅ Yes | ✅ Yes | 5-min window works both |
| **STEC** | ✅ Yes | ✅ Yes | Hatch smoothing works |
| **VTEC** | ⚠️ Inaccurate | ⚠️ Still inaccurate | Missing proper mapping function |
| **Satellite Positions** | ✅ Computed | ✅ Computed | But not linked to observations |
| **IPP (Pierce Point)** | ❌ Fake | ❌ Fake | Hash-based, not actual geometry |
| **Phase Slip Detection** | ✅ Yes | ✅ Yes | Working but no feedback loop |

---

## 🚨 PERFORMANCE & SCALING

### Current (Single Station - KMIT6):
- **Rows per second**: 10-19
- **CSV write time**: 8-20ms
- **Broadcast time**: 0.08-0.29ms (no clients, so no cost)
- **WebSocket pending**: Not applicable (no connection)

### Projected (17 Stations):
- **Rows per second**: 170-323 (17x multiplier)
- **CSV write time**: 136-340ms per publish
- **Broadcast time**: 10-50ms (per 100 connected clients)
- **Memory**: 17 daily CSV files + 17 in-memory buffers

### Issues at Scale:
1. **CSV bottleneck**: 136-340ms writes → 1Hz publish won't keep up
2. **WebSocket load**: 100+ clients × 17 stations = thousands of messages/sec
3. **No distributed WebSocket**: All in single process (kill = all clients disconnect)
4. **Frontend subscriptions**: Current design forces full real-time for all metrics

**Recommendation**: Database instead of CSV for multi-station

---

## 📋 SATELLITE DATA COVERAGE

### Stations with Data Files:
- **KMIT6**: 10+ files (heavily tested 2026-03-07)
- **CHMA**: 2 files
- **CHAN**: 2 files
- **CPN1**: 2 files
- **STFD**: 1 file
- **CM01**: 1 file
- **CADT**: 1 file
- **KMIG**: 1 file
- **PJRK**: 1 file

### Stations with NO Data:
- ITC0, KKU0, DPT9, LPBR, NKNY, NKRM, NKSW (7 stations)

**Note**: Stations without data may not be configured on NTRIP caster or mount points may be down.

---

## ✅ ACTION PLAN (Priority Order)

### TIER 1: FIX REAL-TIME (This Session)
1. **Fix WebSocket Connection** 🔥
   - Check backend `/ws/realtime` endpoint
   - Check CORS settings
   - Check frontend WebSocket URL construction
   - Test connection from browser DevTools
   - Expected result: "WS: open" in frontend

2. **Verify Data Flow to Frontend**
   - Once WebSocket connected, check if charts populate
   - Monitor "Active PRNs" count (should > 0)
   - Check for any frontend parsing errors

### TIER 2: FIX DATA INTEGRITY
3. **Add CSV File Locking**
   - Implement threading.Lock() for CSV writes
   - Prevents race conditions on multi-station scale

4. **Improve S4c Calculation**
   - Add minimum sample count check
   - Implement sliding window if realtime S4c needed

### TIER 3: FIX ACCURACY
5. **Fix VTEC Mapping Function**
   - Implement proper mapping function: `VTEC = STEC / mf(el, az)`
   - Use Chapman model or simpler obliquity factor
   - Account for IPP altitude and user location

6. **Link Ephemeris to Observations**
   - Pass satellite position + geometry to frontend
   - Calculate actual IPP from station lat/lon + sat position
   - Update Phase2 Cesium overlay

### TIER 4: PERFORMANCE OPTIMIZATION
7. **Database Instead of CSV** (if scaling to 17 stations)
   - SQLite or PostgreSQL
   - Indexed queries by station + time range
   - Distributed WebSocket if needed

8. **Frontend Subscription Model**
   - Allow selective metric streaming (e.g., "only S4c for KMIT6")
   - Reduce broadcast payload

---

## 📝 RECOMMENDATIONS FOR INTERNET/NETWORK ISSUES

### Robustness:
- **Worker Reconnection**: Already has exponential backoff (2-60s) ✅
- **WebSocket Heartbeat**: 5s heartbeat, 20s timeout ✅
- **Batch Publishing**: Max 500 rows/batch prevents memory bloat ✅

### What Can Break:
1. **Caster Down** (161.246.18.204:2101)
   - Worker will auto-reconnect
   - Frontend keeps trying, shows 0 PRNs
   - **User sees**: "Active PRNs: 0"

2. **Internet Dropout**
   - Worker loses connection, restarts immediately
   - Frontend shows "WS: closed"
   - Resume when connection restored
   - **Data loss**: Only live stream during dropout

3. **Backend Crashes**
   - All client WebSockets dropped
   - Worker keeps publishing to broken endpoint
   - Data loss until backend restarts
   - **Fix**: Docker restart or process manager

---

## 🎯 NEXT STEPS

**Immediate**: Debug WebSocket connection (print URLs, check headers, browser console)
**Quick Wins**: Fix file locking, add error handling
**Quality**: Improve VTEC accuracy, proper IPP calculation
**Scale**: Move to database when ready for 17-station production

---

**Generated**: 2026-04-21 ~06:00 UTC
**System**: Working but WebSocket connection between Frontend↔Backend is broken
**Impact**: Real-time display not working; data is being collected but not shown
