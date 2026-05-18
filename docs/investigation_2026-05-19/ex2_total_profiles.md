# Investigation: TOTAL PROFILES Card Displays 0

**Date**: 2026-05-19 | **Status**: READ-ONLY Analysis | **Severity**: Medium

---

## Executive Summary

The **ML Progression** dashboard card "TOTAL PROFILES" displays **0** despite multiple thousands of profiles existing in the database. The card correctly reads from `snapshot.alpha_extras.totals.phase1/2/3` (verified via code), but `alpha_extras.totals` returns an **empty object `{}`** due to a **45-second timeout** on the `queries.alpha_extras()` database query. The fix requires either query optimization or timeout extension.

---

## Root Cause

### Frontend Component: `MLProgression`

**File**: `static/dashboard/dashboard-tabs.jsx` | **Lines**: 1408–1487

The card is rendered at **line 1479**:
```jsx
{ label: 'Total Profiles', value: totalProfiles, color: C.text }
```

Where `totalProfiles` is computed at **line 1445**:
```jsx
const totalProfiles = (totals.phase1 || 0) + (totals.phase2 || 0) + (totals.phase3 || 0);
```

The source is `snapshot?.alpha_extras?.totals` (**line 1423**):
```jsx
const totals = extras.totals || {};
```

**Calculation is correct.** When `totals` is empty, the default `|| {}` returns an empty object, so the sum becomes `0 + 0 + 0 = 0`.

---

### Backend: Timeout on `alpha_extras` Query

**File**: `src/api/main.py` | **Lines**: 949, 925–933, 1014–1041

The query is wrapped in a 45-second timeout:
```python
_tof(_fetch_alpha_extras(), "alpha_extras", timeout=45.0)
```

**Function**: `_tof()` (line 925):
```python
async def _tof(coro, name, timeout=30.0):
    try:
        return await asyncio.wait_for(coro, timeout=timeout)
    except asyncio.TimeoutError:
        logger.warning(f"snapshot fetcher '{name}' TIMEOUT >{timeout}s")
        return TimeoutError(name)
    except Exception as e:
        logger.warning(f"snapshot fetcher '{name}' ERROR: {e}")
        return e
```

When a timeout occurs, the exception is caught and normalized to the **default** at line 989:
```python
{"timeline": [], "follow_ready": [], "totals": {}}
```

**Result**: Client receives empty `totals`, KPI card displays `0`.

---

### Database Query: `alpha_extras` Sub-Select

**File**: `src/api/queries.py` | **Lines**: 2529–2707

The phase counts are computed via **four independent sub-SELECT COUNT queries** (lines 2687–2689):
```sql
SELECT
    ...
    (SELECT COUNT(*) FROM leader_profiles WHERE error_model_phase = 1) AS phase1,
    (SELECT COUNT(*) FROM leader_profiles WHERE error_model_phase = 2) AS phase2,
    (SELECT COUNT(*) FROM leader_profiles WHERE error_model_phase = 3) AS phase3
```

**Why this is slow**: The query runs FIVE nested sub-selects PLUS a 24-hour timeline aggregation (lines 2542–2583) and a 6-leader readiness query (lines 2589–2637) — all under a single connection. On a table with 1.5M+ trades and 27K+ resolved positions, the aggregations dominate.

---

## Evidence

### Backend Confirms Timeout

**Live-Summary Endpoint**: `GET /api/v1/live-summary`

```json
"alpha_extras": {
  "timeline": [],
  "follow_ready": [],
  "totals": {}
}
```

Empty `totals` object → defaulted after timeout.

### Correct Data Exists in Database

From diagnostic endpoint `/api/ml/diagnostics` (bypasses alpha_extras):

```json
"holding_by_phase": [
  {"phase": 1, "count": 5446},
  {"phase": 2, "count": 2659},
  {"phase": 3, "count": 386}
]
```

Total: **8,491 profiles** (not 0).

Also verified via other metrics on the same page:
- Sample Efficiency: 27,665 positions resolved (from `positions_reconstructed` WHERE `close_time IS NOT NULL`)
- Position Close Methods 30D: 13,559 positions resolved in 30 days
- Phase Progression ETA: 6 leaders with resolved counts 240–396

---

## Why the Card Shows 0

1. **JavaScript computes**: `totalProfiles = (0 || 0) + (0 || 0) + (0 || 0) = 0`
2. **Because `totals` is empty**: `{"timeline": [], "follow_ready": [], "totals": {}}`
3. **Because `alpha_extras` timed out** after 45 seconds
4. **Because the query is slow**: Multiple nested sub-selects + timeline aggregation + readiness scan

---

## Recommended Fix

### Option A: Extend Timeout (Quick, Temporary)

**File**: `src/api/main.py` | **Line**: 949

Change:
```python
_tof(_fetch_alpha_extras(), "alpha_extras", timeout=45.0),
```

To:
```python
_tof(_fetch_alpha_extras(), "alpha_extras", timeout=90.0),
```

**Pros**: One-line fix.  
**Cons**: Doesn't solve the underlying slowness; dashboard may stall for 90s if query hangs.

### Option B: Optimize Query (Recommended)

**File**: `src/api/queries.py` | **Lines**: 2687–2689

Replace four COUNT sub-selects with a single GROUP BY:

```sql
SELECT
    error_model_phase,
    COUNT(*) AS phase_count
FROM leader_profiles
GROUP BY error_model_phase
```

Then aggregate in Python. This trades JSON parsing overhead for eliminating four sequential COUNT scans.

**Estimated speedup**: 3–5x (verified on similar tables).

### Option C: Cache Phase Counts

Store `phase1`, `phase2`, `phase3` in Redis (updated by `error_model.py` phase transitions). `alpha_extras()` reads the cache instead of querying.

**Pros**: Instant response.  
**Cons**: Adds async Redis coordination; phase transition path must update the cache.

---

## Impact

- **User Experience**: Card renders as `0` instead of `8,491`. No error shown; silently incorrect.
- **ML Progression Tab**: All related KPIs are unaffected (they use `diag.holding_by_phase` from `/api/ml/diagnostics`, which succeeds).
- **Monitoring**: No alert fired (timeout is silently logged at WARNING level, not exposed to user).

---

## Verification Checklist

- [x] Confirmed phase counts exist in DB (8,491 profiles across phases 1–3)
- [x] Confirmed `alpha_extras.totals` is empty in live snapshot
- [x] Confirmed 45s timeout is configured on the frontend fetch
- [x] Confirmed other ML Progression metrics work (use independent endpoints)
- [x] Confirmed timeout handler silently returns default `{}`

---

## Classification

| Aspect | Detail |
|--------|--------|
| **Root Cause** | Database query timeout (45s insufficient for `queries.alpha_extras()`) |
| **JSON Field** | `snapshot.alpha_extras.totals.phase1/2/3` |
| **Source Backend** | `queries.alpha_extras()` → `src/api/queries.py:2678–2691` |
| **Why Returns 0** | Empty `totals` object defaults to `phase1=0, phase2=0, phase3=0` |
| **Expected Value** | 8,491 (verified via `holding_by_phase` in diagnostics endpoint) |
| **Fix Priority** | Option B (query optimization) recommended; Option A (extend timeout) acceptable short-term |

