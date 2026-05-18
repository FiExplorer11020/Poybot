"""
Shared fixtures for the cross-view consistency integration suite.

The tests in this directory hit a running backend over HTTP. They are
intentionally pure-network: no DB or Redis access, no in-process imports
of `src.*`. That keeps them runnable against EITHER:

  * a local dev server (`uvicorn src.api.main:app --port 8000`)
  * the production server at http://89.167.23.215:8080
  * any other deployed instance (override with `POLYBOT_TEST_BASE_URL`)

If no server is reachable the entire suite skips with a clear reason so
CI doesn't fail just because the operator hasn't started the stack.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import httpx
import pytest

# --------------------------------------------------------------------------- #
# Configuration                                                               #
# --------------------------------------------------------------------------- #

# Override via env var so the same test file works locally AND against
# prod. Default to the local uvicorn port — failing to reach it makes
# the suite skip, not error.
BASE_URL = os.environ.get("POLYBOT_TEST_BASE_URL", "http://localhost:8000")

# Endpoints we probe. Add a new one here AND extend `_extract_facts` to
# wire it into the per-fact assertions.
ENDPOINTS = (
    "/api/v1/live-summary",
    "/api/portfolio/pipeline_status",
    "/api/health/pillars",
    "/api/inspector/snapshot",
    "/api/inspector/reconciliation",
    "/api/ml/diagnostics",
    "/api/system",
    "/api/lab/gates",
    "/api/leaders",
)

# Per-endpoint timeout. /api/v1/live-summary is cache-served (<10ms),
# but /api/inspector/snapshot and /api/leaders can take several seconds
# on a cold cache, hence the generous default.
DEFAULT_TIMEOUT_S = 45.0

BASELINE_DIR = Path(__file__).resolve().parents[1] / "baselines"
BASELINE_POST_FIX = BASELINE_DIR / "2026-05-18_post_fix.json"


# --------------------------------------------------------------------------- #
# Skip guard — keep CI green when no backend is up                            #
# --------------------------------------------------------------------------- #

def _server_reachable(base_url: str, timeout_s: float = 3.0) -> tuple[bool, str]:
    """Best-effort GET on /api/portfolio/pipeline_status.

    pipeline_status is cheap (~10ms) and always present in V1, so it's
    our liveness probe. Returns (ok, reason) — when ok=False the suite
    skips with the reason string surfaced to the operator.
    """
    try:
        with httpx.Client(base_url=base_url, timeout=timeout_s) as client:
            r = client.get("/api/portfolio/pipeline_status")
            if r.status_code == 200:
                return True, ""
            return False, f"liveness probe returned HTTP {r.status_code}"
    except httpx.ConnectError as exc:
        return False, f"connect refused: {exc}"
    except httpx.TimeoutException:
        return False, f"timeout after {timeout_s}s"
    except Exception as exc:  # noqa: BLE001 — propagate the diagnostic
        return False, f"unexpected error: {type(exc).__name__}: {exc}"


_REACHABLE, _SKIP_REASON = _server_reachable(BASE_URL)


@pytest.fixture(scope="session")
def base_url() -> str:
    """Expose BASE_URL to test bodies (for log lines and error messages)."""
    return BASE_URL


@pytest.fixture(scope="session")
def skip_if_unreachable() -> None:
    """Skip-marker fixture — call once at the top of every test in this dir."""
    if not _REACHABLE:
        pytest.skip(
            f"Polymarket backend not reachable at {BASE_URL!r}: {_SKIP_REASON}. "
            "Start it via `uvicorn src.api.main:app --port 8000` or set "
            "POLYBOT_TEST_BASE_URL to a reachable instance."
        )


# --------------------------------------------------------------------------- #
# Snapshot fixture — fetch every endpoint ONCE per session                    #
# --------------------------------------------------------------------------- #

async def _fetch_all(base_url: str) -> dict[str, dict[str, Any]]:
    """Fetch every endpoint in `ENDPOINTS` concurrently.

    Returns a dict keyed by endpoint path with:
      {
        "status": int (HTTP status) or "error",
        "elapsed_ms": float,
        "data": dict | None  (None on non-200),
        "error": str | None,
      }
    """
    results: dict[str, dict[str, Any]] = {}
    async with httpx.AsyncClient(base_url=base_url, timeout=DEFAULT_TIMEOUT_S) as client:
        async def _one(ep: str) -> tuple[str, dict[str, Any]]:
            t0 = asyncio.get_event_loop().time()
            try:
                r = await client.get(ep)
                elapsed_ms = round((asyncio.get_event_loop().time() - t0) * 1000, 2)
                # /api/v1/live-summary returns {data: {...}} when populated,
                # OR a 503 with skeleton when warming up. Unwrap the `data`
                # key for live-summary so the rest of the tests can index
                # into it uniformly with the snapshot keys (`stats`, `bot`,
                # `ingestion`, ...).
                payload: Any = None
                if r.status_code == 200:
                    try:
                        body = r.json()
                        if ep == "/api/v1/live-summary" and isinstance(body, dict):
                            # Newer builds return the snapshot at top-level;
                            # older builds nest it under `data`. Accept both.
                            nested = body.get("data")
                            payload = nested if isinstance(nested, dict) else body
                        else:
                            payload = body
                    except Exception as exc:  # noqa: BLE001
                        return ep, {
                            "status": r.status_code,
                            "elapsed_ms": elapsed_ms,
                            "data": None,
                            "error": f"json_parse: {exc}",
                        }
                return ep, {
                    "status": r.status_code,
                    "elapsed_ms": elapsed_ms,
                    "data": payload,
                    "error": None if r.status_code == 200 else f"http_{r.status_code}",
                }
            except Exception as exc:  # noqa: BLE001
                elapsed_ms = round((asyncio.get_event_loop().time() - t0) * 1000, 2)
                return ep, {
                    "status": "error",
                    "elapsed_ms": elapsed_ms,
                    "data": None,
                    "error": f"{type(exc).__name__}: {exc}",
                }

        coros = [_one(ep) for ep in ENDPOINTS]
        for fut in await asyncio.gather(*coros, return_exceptions=False):
            ep, payload = fut
            results[ep] = payload
    return results


@pytest.fixture(scope="session")
async def snapshots(skip_if_unreachable, base_url: str) -> dict[str, dict[str, Any]]:
    """One-shot fetch of all endpoints, reused across the suite.

    The cross-view consistency tests MUST read the same physical
    response — if we re-fetched per test, a fast-moving counter
    (observed_trades_24h, decisions_24h, ...) could legitimately tick
    between requests and produce false-positive divergences. Capturing
    once at session-start gives us a stable point-in-time vector.
    """
    return await _fetch_all(base_url)
