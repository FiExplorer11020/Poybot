"""
Cross-view consistency integration tests (Agent A13 — anti-regression safety net).

PURPOSE
-------
After the 6-batch cross-view refactor (A2-A12), each observable fact
exposed by the V1 dashboard must come from EXACTLY ONE producer and be
mirrored to every consumer route under the SAME numeric value. Without
this, future PRs that wire a new endpoint to a divergent source can
silently re-introduce the kind of bug audited in
`AUDIT_PROBLEMES_TECHNIQUES_2026_05_18.md` (RECON drift between cards,
DECISIONS 24h reading 0, topbar showing the wrong WS lag, etc.).

For each canonical fact:

  1. Collect the value from every route that claims to expose it.
  2. Tolerate a route returning None / missing (feature flag OFF,
     migration pending) — but if >=2 routes respond, they MUST agree
     under a small numeric tolerance.

The suite captures a post-fix baseline JSON at the end (no assertion
beyond "file written"), which we diff against the pre-fix baseline in
`tests/baselines/2026-05-18_pre_fix.json` to document the delta.

RUN
---
    # Local backend (default)
    pytest tests/integration/test_cross_view_consistency.py -v -m integration

    # Against prod
    POLYBOT_TEST_BASE_URL=http://89.167.23.215:8080 \
        pytest tests/integration/test_cross_view_consistency.py -v -m integration

If no backend is reachable the entire module skips with a clear reason
(see conftest.py).
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

# Every test in this file is an integration test — pytest -m integration
# selects this module, pytest -m "not integration" excludes it.
pytestmark = pytest.mark.integration


# --------------------------------------------------------------------------- #
# Comparators                                                                 #
# --------------------------------------------------------------------------- #

def _almost_equal(a: Any, b: Any, rtol: float = 0.005, atol: float = 1.0) -> bool:
    """Loose equality for cross-view numeric checks.

    The tolerance defaults (rtol=0.5%, atol=1) absorb the small drift
    that can happen when two endpoints compute the same fact at
    slightly different timestamps (counters tick, MAX(time) shifts by a
    few ms). For status strings the comparison is exact.
    """
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    if isinstance(a, bool) or isinstance(b, bool):
        return a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(a - b) <= max(atol, rtol * max(abs(a), abs(b)))
    return a == b


def _agree(values: list[Any]) -> tuple[bool, str]:
    """True iff every non-None value in `values` is pairwise _almost_equal.

    Returns (ok, reason). On disagreement, `reason` lists the divergent
    pairs so the failure message points the reader at the right
    endpoints.
    """
    seen = [v for v in values if v is not None]
    if len(seen) < 2:
        # Not enough sources to compare — caller decides whether to
        # treat that as "skip" or "fail must-have".
        return True, "fewer than 2 non-None sources"
    for i, a in enumerate(seen):
        for j, b in enumerate(seen[i + 1:], start=i + 1):
            if not _almost_equal(a, b):
                return False, f"index {i} ({a!r}) != index {j} ({b!r})"
    return True, "all sources agree"


# --------------------------------------------------------------------------- #
# Extractors — one per canonical fact                                         #
# --------------------------------------------------------------------------- #

def _live_summary(snapshots: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Return the snapshot body (already unwrapped from {data:...} by conftest)."""
    raw = snapshots.get("/api/v1/live-summary") or {}
    return raw.get("data") or {}


def _pipeline_status(snapshots: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return (snapshots.get("/api/portfolio/pipeline_status") or {}).get("data") or {}


def _ml_diagnostics(snapshots: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return (snapshots.get("/api/ml/diagnostics") or {}).get("data") or {}


def _inspector_recon(snapshots: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return (snapshots.get("/api/inspector/reconciliation") or {}).get("data") or {}


def _inspector_snapshot(snapshots: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return (snapshots.get("/api/inspector/snapshot") or {}).get("data") or {}


# --------------------------------------------------------------------------- #
# Tests — one per canonical fact (8 total)                                    #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_observed_trades_24h_consistent(snapshots):
    """observed_trades_24h (firehose count) must agree across all sources.

    Post-fix expected exposure:
      * snapshot.observed_trades_24h         (terminal_snapshot top-level)
      * snapshot.stats.observed_trades_24h   (terminal_snapshot stats mirror)
      * snapshot.ingestion.observed_trades_24h (not yet wired in V1; tolerate None)
      * pipeline_status.observed_trades_24h  (queries.portfolio_pipeline_status)
      * ml/diagnostics.sample_efficiency.trades_observed_total (DB-side cumulative)

    NOTE: ml/diagnostics.trades_observed_total is a CUMULATIVE wallet-side
    sum (not a 24h count), so we exclude it from the agreement check —
    it would always diverge by 2-3 orders of magnitude. The check focuses
    on the Redis-backed 24h counter mirrored to live-summary + pipeline_status.
    """
    snap = _live_summary(snapshots)
    pipe = _pipeline_status(snapshots)

    val_snapshot_top = snap.get("observed_trades_24h")
    val_snapshot_stats = (snap.get("stats") or {}).get("observed_trades_24h")
    val_snapshot_ing = (snap.get("ingestion") or {}).get("observed_trades_24h")
    val_pipeline = pipe.get("observed_trades_24h")

    candidates = {
        "snapshot.observed_trades_24h": val_snapshot_top,
        "snapshot.stats.observed_trades_24h": val_snapshot_stats,
        "snapshot.ingestion.observed_trades_24h": val_snapshot_ing,
        "pipeline_status.observed_trades_24h": val_pipeline,
    }
    non_null = {k: v for k, v in candidates.items() if v is not None}
    if len(non_null) < 2:
        pytest.skip(
            f"Only {len(non_null)} source(s) expose observed_trades_24h "
            f"(post-fix code not yet deployed?). Saw: {candidates}"
        )
    ok, reason = _agree(list(non_null.values()))
    assert ok, f"observed_trades_24h divergent across {list(non_null.keys())}: {reason}"


@pytest.mark.asyncio
async def test_exec_trades_24h_consistent(snapshots):
    """exec_trades_24h (paper-bot opens last 24h) must agree across mirrors.

    Post-fix expected exposure:
      * snapshot.exec_trades_24h        (terminal_snapshot top-level)
      * snapshot.stats.exec_trades_24h  (terminal_snapshot stats mirror)
    """
    snap = _live_summary(snapshots)
    candidates = {
        "snapshot.exec_trades_24h": snap.get("exec_trades_24h"),
        "snapshot.stats.exec_trades_24h": (snap.get("stats") or {}).get("exec_trades_24h"),
    }
    non_null = {k: v for k, v in candidates.items() if v is not None}
    if len(non_null) < 2:
        pytest.skip(
            f"Only {len(non_null)} source(s) expose exec_trades_24h "
            f"(post-fix code not yet deployed?). Saw: {candidates}"
        )
    ok, reason = _agree(list(non_null.values()))
    assert ok, (
        f"exec_trades_24h divergent across {list(non_null.keys())}: {reason}"
    )


@pytest.mark.asyncio
async def test_bot_status_canonical_consistent(snapshots):
    """bot_status (canonical UPPERCASE) must agree between snapshot.bot and pipeline_status.

    Post-fix expected exposure:
      * snapshot.bot.bot_status                  (terminal_snapshot _build_bot_payload)
      * pipeline_status.bot_status_canonical     (queries.portfolio_pipeline_status)

    Both should be in {RUNNING, STOPPED, DEGRADED}. The legacy lowercase
    `pipeline_status.bot_status` is kept for back-compat and is NOT
    expected to match (legacy = healthy/down/paused, canonical = uppercase).
    """
    snap = _live_summary(snapshots)
    pipe = _pipeline_status(snapshots)

    val_snap_bot = (snap.get("bot") or {}).get("bot_status")
    val_pipe_canonical = pipe.get("bot_status_canonical")

    candidates = {
        "snapshot.bot.bot_status": val_snap_bot,
        "pipeline_status.bot_status_canonical": val_pipe_canonical,
    }
    non_null = {k: v for k, v in candidates.items() if v is not None}
    if len(non_null) < 2:
        pytest.skip(
            f"Only {len(non_null)} canonical bot_status source(s) "
            f"available (post-fix code not yet deployed?). Saw: {candidates}"
        )
    ok, reason = _agree(list(non_null.values()))
    assert ok, f"bot_status divergent: {reason}"
    # Sanity: the canonical value must be in the expected set.
    for k, v in non_null.items():
        assert v in {"RUNNING", "STOPPED", "DEGRADED"}, (
            f"{k} = {v!r}, expected RUNNING|STOPPED|DEGRADED"
        )


@pytest.mark.asyncio
async def test_ws_status_canonical_consistent(snapshots):
    """ws_status (canonical UPPERCASE) must agree across mirrors.

    Post-fix expected exposure:
      * snapshot.bot.ws_status               (terminal_snapshot)
      * pipeline_status.ws_status_canonical  (queries.portfolio_pipeline_status)
    """
    snap = _live_summary(snapshots)
    pipe = _pipeline_status(snapshots)

    val_snap = (snap.get("bot") or {}).get("ws_status")
    val_pipe_canonical = pipe.get("ws_status_canonical")

    candidates = {
        "snapshot.bot.ws_status": val_snap,
        "pipeline_status.ws_status_canonical": val_pipe_canonical,
    }
    non_null = {k: v for k, v in candidates.items() if v is not None}
    if len(non_null) < 2:
        pytest.skip(
            f"Only {len(non_null)} canonical ws_status source(s) "
            f"available (post-fix code not yet deployed?). Saw: {candidates}"
        )
    ok, reason = _agree(list(non_null.values()))
    assert ok, f"ws_status divergent: {reason}"
    for k, v in non_null.items():
        assert v in {"LIVE", "DEGRADED", "DOWN"}, (
            f"{k} = {v!r}, expected LIVE|DEGRADED|DOWN"
        )


@pytest.mark.asyncio
async def test_ws_last_message_age_consistent(snapshots):
    """The TRUE WS lag (ws_last_message_age_s) must agree across mirrors.

    Pre-refactor the topbar conflated `bot.latency_ms` (60-market book
    freshness average, dominated by stale markets) with the real WS lag
    (`ws:market:last_message_ts` age). Post-fix:

      * snapshot.ingestion.ws_last_message_age_s   (terminal_snapshot._build_ingestion)
      * pipeline_status.ws_last_message_age_s      (queries.portfolio_pipeline_status)

    Tolerance bumped to 5s atol because the two endpoints can re-read
    the Redis TS up to a few seconds apart.
    """
    snap = _live_summary(snapshots)
    pipe = _pipeline_status(snapshots)

    val_snap = (snap.get("ingestion") or {}).get("ws_last_message_age_s")
    val_pipe = pipe.get("ws_last_message_age_s")

    candidates = {
        "snapshot.ingestion.ws_last_message_age_s": val_snap,
        "pipeline_status.ws_last_message_age_s": val_pipe,
    }
    non_null = {k: v for k, v in candidates.items() if v is not None}
    if len(non_null) < 2:
        pytest.skip(
            f"Only {len(non_null)} source(s) expose ws_last_message_age_s "
            f"(post-fix code not yet deployed?). Saw: {candidates}"
        )
    # Wider absolute tolerance — these are point-in-time samples that
    # can shift by a few seconds between the two reads.
    ok, reason = _agree(list(non_null.values()))
    if not ok:
        # Re-check with the looser tolerance suited to this fact.
        seen = list(non_null.values())
        ok = all(
            _almost_equal(a, b, rtol=0.05, atol=5.0)
            for i, a in enumerate(seen) for b in seen[i + 1:]
        )
        reason = f"diff outside 5s atol: {seen}"
    assert ok, f"ws_last_message_age_s divergent: {reason}"


@pytest.mark.asyncio
async def test_paper_pnl_consistent(snapshots):
    """The displayed paper PnL must agree between the snapshot and reconciliation.

    Post-fix expected exposure:
      * snapshot.stats.total_pnl                     (terminal_snapshot)
      * inspector/reconciliation.pnl_displayed_sum   (reconciliation_queries)
    """
    snap = _live_summary(snapshots)
    recon = _inspector_recon(snapshots)

    val_snap = (snap.get("stats") or {}).get("total_pnl")
    val_recon = recon.get("pnl_displayed_sum")

    candidates = {
        "snapshot.stats.total_pnl": val_snap,
        "inspector/reconciliation.pnl_displayed_sum": val_recon,
    }
    non_null = {k: v for k, v in candidates.items() if v is not None}
    if len(non_null) < 2:
        pytest.skip(
            f"Only {len(non_null)} source(s) expose paper PnL "
            f"(post-fix code not yet deployed?). Saw: {candidates}"
        )
    # PnL tolerance: 1 USDC absolute or 0.5% relative. Two reads of
    # paper_trades can disagree by a single freshly-closed position.
    ok, reason = _agree(list(non_null.values()))
    assert ok, f"paper_pnl divergent: {reason}"


@pytest.mark.asyncio
async def test_reconciliation_verdict_consistent(snapshots):
    """The reconciliation verdict must agree across mirrors.

    Post-fix expected exposure:
      * snapshot.bot.reconciliation.verdict          (terminal_snapshot._build_bot_payload)
      * snapshot.reconciliation.verdict              (terminal_snapshot top-level)
      * inspector/reconciliation.verdict             (reconciliation_queries)
    """
    snap = _live_summary(snapshots)
    recon = _inspector_recon(snapshots)

    val_bot = ((snap.get("bot") or {}).get("reconciliation") or {}).get("verdict")
    val_top = (snap.get("reconciliation") or {}).get("verdict")
    val_recon = recon.get("verdict")

    candidates = {
        "snapshot.bot.reconciliation.verdict": val_bot,
        "snapshot.reconciliation.verdict": val_top,
        "inspector/reconciliation.verdict": val_recon,
    }
    # "unknown" is the safe default when reconciliation hasn't run yet;
    # treat it as a non-source for the agreement check (we can't tell
    # whether two "unknown"s came from a real read or a fallback path).
    non_null = {
        k: v for k, v in candidates.items()
        if v is not None and v != "unknown"
    }
    if len(non_null) < 2:
        pytest.skip(
            f"Only {len(non_null)} source(s) expose a concrete recon verdict "
            f"(post-fix code not yet deployed?). Saw: {candidates}"
        )
    ok, reason = _agree(list(non_null.values()))
    assert ok, f"reconciliation_verdict divergent: {reason}"
    for k, v in non_null.items():
        assert v in {"ok", "warn", "critical"}, (
            f"{k} = {v!r}, expected ok|warn|critical"
        )


@pytest.mark.asyncio
async def test_decisions_24h_total_consistent(snapshots):
    """decisions_24h.total must agree between ml/diagnostics and inspector counters.

    Post-fix expected exposure:
      * ml/diagnostics.decisions_24h.total           (queries.ml_diagnostics)
      * inspector/snapshot.counters.decisions_1h     (RELATED but NOT identical — 1h window)
      * inspector/reconciliation.decisions.total     (if/when wired through recon)

    The 1h vs 24h windows are explicitly DIFFERENT facts (the dashboard
    renders both side-by-side), so this test only enforces agreement
    between sources that claim the SAME window (24h). The inspector
    counters_1h is read separately by other tests / panels.
    """
    ml = _ml_diagnostics(snapshots)
    recon = _inspector_recon(snapshots)

    val_ml = ((ml.get("decisions_24h") or {}).get("total"))
    # The reconciliation surface in CLAUDE.md mentions a future
    # `decisions.total` mirror; tolerate it being absent today.
    val_recon = ((recon.get("decisions") or {}).get("total"))

    candidates = {
        "ml/diagnostics.decisions_24h.total": val_ml,
        "inspector/reconciliation.decisions.total": val_recon,
    }
    non_null = {k: v for k, v in candidates.items() if v is not None}
    if len(non_null) < 2:
        pytest.skip(
            f"Only {len(non_null)} source(s) expose decisions_24h.total "
            f"(post-fix mirror not yet wired). Saw: {candidates}"
        )
    ok, reason = _agree(list(non_null.values()))
    assert ok, f"decisions_24h.total divergent: {reason}"


# --------------------------------------------------------------------------- #
# Bonus — capture post-fix baseline                                           #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_capture_post_fix_baseline(snapshots, base_url, tmp_path):  # noqa: ARG001
    """Capture the post-fix baseline so we can diff it against pre-fix.

    Writes `tests/baselines/2026-05-18_post_fix.json` with every
    endpoint's response + a per-fact extraction table. Not strictly an
    assertion test — but it FAILS if the file can't be written so the
    operator notices.
    """
    snap = _live_summary(snapshots)
    pipe = _pipeline_status(snapshots)
    ml = _ml_diagnostics(snapshots)
    recon = _inspector_recon(snapshots)

    snap_stats = snap.get("stats") or {}
    snap_ing = snap.get("ingestion") or {}
    snap_bot = snap.get("bot") or {}
    ml_eff = ml.get("sample_efficiency") or {}

    facts_cross_view: dict[str, dict[str, Any]] = {
        "observed_trades_24h": {
            "snapshot.observed_trades_24h": snap.get("observed_trades_24h"),
            "snapshot.stats.observed_trades_24h": snap_stats.get("observed_trades_24h"),
            "snapshot.ingestion.observed_trades_24h": snap_ing.get("observed_trades_24h"),
            "pipeline_status.observed_trades_24h": pipe.get("observed_trades_24h"),
            "ml/diagnostics.sample_efficiency.trades_observed_total": (
                ml_eff.get("trades_observed_total")
            ),
        },
        "exec_trades_24h": {
            "snapshot.exec_trades_24h": snap.get("exec_trades_24h"),
            "snapshot.stats.exec_trades_24h": snap_stats.get("exec_trades_24h"),
        },
        "bot_status": {
            "snapshot.bot.bot_status": snap_bot.get("bot_status"),
            "pipeline_status.bot_status_canonical": pipe.get("bot_status_canonical"),
            "pipeline_status.bot_status_legacy": pipe.get("bot_status"),
        },
        "ws_status": {
            "snapshot.bot.ws_status": snap_bot.get("ws_status"),
            "pipeline_status.ws_status_canonical": pipe.get("ws_status_canonical"),
            "pipeline_status.ws_status_legacy": pipe.get("ws_status"),
        },
        "ws_last_message_age_s": {
            "snapshot.ingestion.ws_last_message_age_s": snap_ing.get("ws_last_message_age_s"),
            "pipeline_status.ws_last_message_age_s": pipe.get("ws_last_message_age_s"),
        },
        "paper_pnl": {
            "snapshot.stats.total_pnl": snap_stats.get("total_pnl"),
            "inspector/reconciliation.pnl_displayed_sum": recon.get("pnl_displayed_sum"),
        },
        "reconciliation_verdict": {
            "snapshot.bot.reconciliation.verdict": (
                (snap_bot.get("reconciliation") or {}).get("verdict")
            ),
            "snapshot.reconciliation.verdict": (snap.get("reconciliation") or {}).get("verdict"),
            "inspector/reconciliation.verdict": recon.get("verdict"),
        },
        "decisions_24h": {
            "ml/diagnostics.decisions_24h.total": (ml.get("decisions_24h") or {}).get("total"),
            "inspector/reconciliation.decisions.total": (recon.get("decisions") or {}).get("total"),
        },
    }

    # Endpoint summary — status + small subset of keys (NOT the full
    # payload, which would balloon the file to MB-scale).
    endpoint_summary: dict[str, dict[str, Any]] = {}
    for ep, raw in snapshots.items():
        endpoint_summary[ep] = {
            "status": raw.get("status"),
            "elapsed_ms": raw.get("elapsed_ms"),
            "error": raw.get("error"),
            "top_keys": (
                sorted(list((raw.get("data") or {}).keys()))[:50]
                if isinstance(raw.get("data"), dict) else None
            ),
        }

    baseline = {
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "base_url": base_url,
        "compared_to_baseline": "2026-05-18_pre_fix.json",
        "endpoints": endpoint_summary,
        "facts_cross_view": facts_cross_view,
        "notes": {
            "purpose": (
                "Captured by tests/integration/test_cross_view_consistency.py "
                "(Agent A13 anti-regression safety net). Each test in that file "
                "asserts that ≥2 sources for the same fact agree under a small "
                "tolerance — this file records the observed values at run time."
            ),
            "fact_count": len(facts_cross_view),
            "tolerance": "rtol=0.5%, atol=1.0 (paper_pnl) / atol=5s (ws_lag)",
        },
    }

    baseline_dir = Path(__file__).resolve().parents[1] / "baselines"
    baseline_dir.mkdir(parents=True, exist_ok=True)
    out_path = baseline_dir / "2026-05-18_post_fix.json"
    out_path.write_text(json.dumps(baseline, indent=2, default=str))
    assert out_path.exists(), f"failed to write {out_path}"
    # Sanity: the file must round-trip through json.load to confirm it
    # didn't end up with NaN/Inf/non-serializable garbage.
    json.loads(out_path.read_text())
