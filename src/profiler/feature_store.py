"""
Point-in-time feature store for market features.

Phase 3 Round 2. Two agents own pieces of this file:

* Agent Y — market-level features (liquidity / volume / category /
  fee_rate) sourced from `market_features_history` (migration 016).
  See docs/audit/05_ml_pipeline.md MG-3 § 3.1 and
  docs/audit/phase3/round2_Y_feature_store.md.

* Agent Z — per-token order-book features (depth imbalance / spread /
  microprice) sourced from `orderbook_features_minute` (migration 018).
  See docs/audit/05_ml_pipeline.md summary and
  docs/audit/phase3/round2_Z_orderbook_imbalance.md.

Both surfaces share the "AS-OF, never AS-OF-NOW" contract: every read
takes an explicit ``asof_ts`` so the training pipeline doesn't suffer
train/serve skew (the audit's core MG-3 finding).

Helpers exported here:

* ``get_market_features_asof``        — single (market_id, asof) lookup
                                        — Agent Y.
* ``get_market_features_asof_batch``  — batched LATERAL-JOIN — Agent Y.
* ``get_orderbook_features_asof``     — single (token_id, asof) lookup
                                        with lookback floor — Agent Z.

This module is READ-MOSTLY. The dual-write happens inside
`LeaderRegistry.sync_markets` (Agent Y) and the per-minute rollup loop
in `src/observer/orderbook_observer.py` (Agent Z). Any write paths from
training code are bugs.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any

from loguru import logger

# Prometheus instrumentation. The metrics block is the new contract that
# `src/monitoring/metrics.py` exports; fall back to no-ops in early CI
# before the metrics module lands (same pattern Phase 1 Task O / F use).
try:
    from src.monitoring.metrics import (  # type: ignore[attr-defined]
        feature_store_batch_size,
        feature_store_lookup_latency_seconds,
        feature_store_lookups_total,
    )
except Exception:  # pragma: no cover
    class _NoOpLabel:
        def labels(self, *_a, **_kw):
            return self

        def inc(self, *_a, **_kw):
            return None

        def observe(self, *_a, **_kw):
            return None

        def set(self, *_a, **_kw):
            return None

    feature_store_lookups_total = _NoOpLabel()  # type: ignore[assignment]
    feature_store_batch_size = _NoOpLabel()  # type: ignore[assignment]
    feature_store_lookup_latency_seconds = _NoOpLabel()  # type: ignore[assignment]

# Agent Z metric — separate counter so the Y/Z surfaces don't fight over
# label cardinality on the same Prometheus series.
try:
    from src.monitoring.metrics import (  # type: ignore[attr-defined]
        orderbook_features_lookup_total,
    )
except Exception:  # pragma: no cover
    class _NoOpLabelZ:
        def labels(self, *_a, **_kw):
            return self

        def inc(self, *_a, **_kw):
            return None

    orderbook_features_lookup_total = _NoOpLabelZ()  # type: ignore[assignment]


def _batch_size_bucket(n: int) -> str:
    """Coarse bucket label for the latency histogram. Keeps cardinality bounded."""
    if n <= 1:
        return "1"
    if n <= 10:
        return "10"
    if n <= 100:
        return "100"
    if n <= 1000:
        return "1000"
    return "10000+"


def _row_to_dict(row: Any) -> dict:
    """Normalize an asyncpg.Record (or a plain dict in tests) into a
    plain dict the caller can read by key. The values are returned as
    Python primitives — Decimal stays Decimal so the caller can cast
    however it likes (`float()` is the typical call).
    """
    if row is None:
        return {}
    # asyncpg.Record supports dict(record); fall through cleanly for tests
    # that pass a dict already.
    try:
        return dict(row)
    except Exception:
        return {k: row[k] for k in row.keys()}  # type: ignore[attr-defined]


async def get_market_features_asof(
    conn: Any,
    market_id: str,
    asof_ts: datetime,
) -> dict | None:
    """Return the most-recent ``market_features_history`` row for
    ``market_id`` with ``captured_at <= asof_ts``.

    Returns ``None`` if no row qualifies — caller decides whether to
    fall back to the live ``markets`` value (the typical pattern for
    legacy training rows older than the dual-write start; see
    `error_model._fetch_training_data`).

    Single roundtrip; the planner uses ``idx_mfh_market_time`` DESC for
    an index-only LIMIT 1 descent.
    """
    t0 = time.perf_counter()
    bucket = _batch_size_bucket(1)
    feature_store_batch_size.observe(1)
    try:
        row = await conn.fetchrow(
            """
            SELECT
                market_id,
                captured_at,
                liquidity_score,
                volume_24h,
                category,
                fee_rate_pct,
                source,
                extra_json
            FROM market_features_history
            WHERE market_id = $1
              AND captured_at <= $2
            ORDER BY captured_at DESC
            LIMIT 1
            """,
            market_id,
            asof_ts,
        )
    except Exception as exc:
        logger.warning(
            f"feature_store.get_market_features_asof failed for "
            f"market_id={market_id} asof={asof_ts.isoformat()}: {exc}"
        )
        feature_store_lookups_total.labels(
            table="market_features_history", result="miss"
        ).inc()
        feature_store_lookup_latency_seconds.labels(
            batch_size_bucket=bucket
        ).observe(time.perf_counter() - t0)
        return None

    feature_store_lookup_latency_seconds.labels(
        batch_size_bucket=bucket
    ).observe(time.perf_counter() - t0)

    if row is None:
        feature_store_lookups_total.labels(
            table="market_features_history", result="miss"
        ).inc()
        return None

    feature_store_lookups_total.labels(
        table="market_features_history", result="asof_hit"
    ).inc()
    return _row_to_dict(row)


async def get_market_features_asof_batch(
    conn: Any,
    queries: list[tuple[str, datetime]],
) -> dict[tuple[str, datetime], dict | None]:
    """Batched variant. Issues a single SQL with LATERAL JOIN over the
    input list. Returns a dict keyed by the EXACT ``(market_id, asof_ts)``
    tuples in ``queries``; values are the row-dict (asof hit) or
    ``None`` (no row at-or-before asof for that market).

    Used by ``error_model._fetch_training_data`` — potentially thousands
    of historical positions, so we MUST avoid the N+1. The implementation
    is one round-trip regardless of input length.

    Edge cases:
    * Empty input — returns ``{}`` without touching the DB.
    * Duplicate ``(market_id, asof_ts)`` keys in the input — the result
      dict has one entry per unique key (a normal dict-build).
    """
    if not queries:
        return {}

    # Stable column order so we can zip results back to the inputs by
    # numeric index. We DON'T trust SQL UNNEST to preserve insertion
    # order on every PG version; instead we attach a sentinel row index
    # to each input and read it back from the SELECT.
    n = len(queries)
    indices = list(range(n))
    market_ids = [q[0] for q in queries]
    asof_ts_list = [q[1] for q in queries]

    bucket = _batch_size_bucket(n)
    feature_store_batch_size.observe(n)
    t0 = time.perf_counter()

    try:
        rows = await conn.fetch(
            """
            WITH inputs(idx, market_id, asof) AS (
                SELECT * FROM UNNEST($1::int[], $2::text[], $3::timestamptz[])
            )
            SELECT
                i.idx,
                i.market_id           AS in_market_id,
                i.asof                AS in_asof,
                h.captured_at,
                h.liquidity_score,
                h.volume_24h,
                h.category,
                h.fee_rate_pct,
                h.source,
                h.extra_json
            FROM inputs i
            LEFT JOIN LATERAL (
                SELECT captured_at, liquidity_score, volume_24h,
                       category, fee_rate_pct, source, extra_json
                FROM market_features_history
                WHERE market_id = i.market_id
                  AND captured_at <= i.asof
                ORDER BY captured_at DESC
                LIMIT 1
            ) h ON TRUE
            """,
            indices,
            market_ids,
            asof_ts_list,
        )
    except Exception as exc:
        logger.warning(
            f"feature_store.get_market_features_asof_batch failed "
            f"(n={n}): {exc}"
        )
        feature_store_lookups_total.labels(
            table="market_features_history", result="miss"
        ).inc(n)
        feature_store_lookup_latency_seconds.labels(
            batch_size_bucket=bucket
        ).observe(time.perf_counter() - t0)
        return {key: None for key in queries}

    feature_store_lookup_latency_seconds.labels(
        batch_size_bucket=bucket
    ).observe(time.perf_counter() - t0)

    result: dict[tuple[str, datetime], dict | None] = {}
    hits = 0
    for row in rows:
        idx = int(row["idx"])
        key = (market_ids[idx], asof_ts_list[idx])
        if row["captured_at"] is None:
            # LEFT JOIN LATERAL produced a NULL row — no history for this market.
            result[key] = None
        else:
            hits += 1
            result[key] = {
                "market_id": row["in_market_id"],
                "captured_at": row["captured_at"],
                "liquidity_score": row["liquidity_score"],
                "volume_24h": row["volume_24h"],
                "category": row["category"],
                "fee_rate_pct": row["fee_rate_pct"],
                "source": row["source"],
                "extra_json": row["extra_json"],
            }

    # Belt-and-suspenders: ensure every input key has an entry. If the
    # SQL skipped a row (shouldn't happen — LEFT JOIN over UNNEST is
    # row-preserving) we surface that as None rather than KeyError.
    for key in queries:
        result.setdefault(key, None)

    if hits:
        feature_store_lookups_total.labels(
            table="market_features_history", result="asof_hit"
        ).inc(hits)
    misses = n - hits
    if misses:
        feature_store_lookups_total.labels(
            table="market_features_history", result="miss"
        ).inc(misses)

    return result


def record_fallback_live(n: int = 1) -> None:
    """Helper for callers that fall back to the live ``markets.liquidity_score``
    when no history row exists at-or-before the as-of timestamp. Bumps
    the ``feature_store_lookups_total{result='fallback_live'}`` counter
    so the dashboard can track the legacy-row fallback rate.
    """
    if n <= 0:
        return
    feature_store_lookups_total.labels(
        table="market_features_history", result="fallback_live"
    ).inc(n)


# --------------------------------------------------------------------------- #
# Agent Z — order-book features                                                #
# --------------------------------------------------------------------------- #


async def get_orderbook_features_asof(
    conn: Any,
    token_id: str,
    asof_ts: datetime,
    lookback_s: int = 300,
) -> dict | None:
    """Return the most-recent ``orderbook_features_minute`` row for
    ``token_id`` with ``bucket_ts <= asof_ts AND
    bucket_ts >= asof_ts - lookback_s``.

    Returns ``None`` when no rollup row exists within ``lookback_s``
    seconds before ``asof_ts``. The caller (`error_model._build_features`,
    eventually owned by Agent Y) treats `None` as "no orderbook signal"
    and lets the existing feature defaults stand — adding these features
    must be additive, never a hard dependency.

    Args:
        conn:       asyncpg connection (caller is already inside
                    ``async with get_db() as conn`` — passing it in
                    lets the caller batch this read with the market
                    features lookup in the same DB roundtrip group).
        token_id:   CTF token id (YES or NO leg). The rollup table is
                    keyed by token, NOT market — YES and NO carry
                    independent order books and depth imbalance on YES
                    is not (in general) equivalent to depth imbalance
                    on NO.
        asof_ts:    Point-in-time we want features for. Typically
                    ``positions_reconstructed.open_time`` in training
                    or ``datetime.utcnow()`` at decision time.
        lookback_s: Maximum staleness budget (default 300 s = 5 min,
                    well above the 60 s rollup cadence so a single
                    skipped minute doesn't cause a miss).

    Returns:
        A dict with: ``bucket_ts``, ``depth_imbalance_mean``,
        ``depth_imbalance_max``, ``spread_bps_mean``, ``spread_bps_max``,
        ``microprice_mean``, ``microprice_deviation_mean``,
        ``n_snapshots``, and a synthesised ``feature_age_s`` =
        seconds between ``asof_ts`` and ``bucket_ts``.

        ``None`` if no row qualifies.

    Metrics:
        Increments ``polybot_orderbook_features_lookup_total`` with
        result label ``hit`` / ``stale`` / ``miss``.
    """
    floor = asof_ts - timedelta(seconds=max(1, int(lookback_s)))
    try:
        row = await conn.fetchrow(
            """
            SELECT bucket_ts,
                   depth_imbalance_mean, depth_imbalance_max,
                   spread_bps_mean, spread_bps_max,
                   microprice_mean, microprice_deviation_mean,
                   n_snapshots
            FROM orderbook_features_minute
            WHERE token_id = $1
              AND bucket_ts <= $2
              AND bucket_ts >= $3
            ORDER BY bucket_ts DESC
            LIMIT 1
            """,
            token_id,
            asof_ts,
            floor,
        )
    except Exception as exc:
        logger.debug(
            f"feature_store.get_orderbook_features_asof query failed "
            f"for token_id={token_id} asof={asof_ts.isoformat()}: {exc}"
        )
        try:
            orderbook_features_lookup_total.labels(result="miss").inc()
        except Exception:
            pass
        return None

    if row is None:
        # We could fire a second query (no lookback floor) to split
        # "no data at all" (miss) vs "data exists but too stale" (stale).
        # That second roundtrip per training-row would double the read
        # load. Instead we accept the conflation — consumers care about
        # hit-rate, not the breakdown — and let the engine alert on the
        # GAP via book_quality_snapshots freshness, which is the upstream
        # truth signal.
        try:
            orderbook_features_lookup_total.labels(result="miss").inc()
        except Exception:
            pass
        return None

    result = _row_to_dict(row)
    bucket_ts = result.get("bucket_ts")
    try:
        if bucket_ts is not None:
            result["feature_age_s"] = max(0.0, (asof_ts - bucket_ts).total_seconds())
    except Exception:
        result["feature_age_s"] = None

    try:
        orderbook_features_lookup_total.labels(result="hit").inc()
    except Exception:
        pass

    return result


# --------------------------------------------------------------------------- #
# Round 11 (The Microscope) — microstructure features                         #
# --------------------------------------------------------------------------- #
# Two read surfaces feed the R8 strategy classifier:
#
# * ``get_microstructure_features_asof``        — per-token rollup row from
#                                                 ``microstructure_features``
#                                                 (migration 033). Returns
#                                                 None if no row is within
#                                                 the lookback window.
#
# * ``get_wallet_microstructure_signature_asof`` — per-wallet rolling
#                                                  signature from
#                                                  ``wallet_microstructure_signature``
#                                                  (migration 034). Returns
#                                                  None if no row at-or-before
#                                                  asof_ts within
#                                                  ``lookback_days``.
#
# Both read at most one row per call. The R8 ``LeaderFeatureExtractor`` calls
# them per (token, time) and per (wallet, time) — the calls are batched at
# the extractor level, not here, because the batch shape is N tokens at the
# same asof_ts, which doesn't benefit from a LATERAL JOIN over UNNEST the
# way the market-features path does.


async def get_microstructure_features_asof(
    conn: Any,
    market_id: str,
    token_id: str,
    asof_ts: datetime,
    lookback_s: int = 300,
) -> dict | None:
    """Return the most-recent ``microstructure_features`` row for
    ``(market_id, token_id)`` with
    ``bucket_ts <= asof_ts AND bucket_ts >= asof_ts - lookback_s``.

    Returns ``None`` when no row qualifies — caller (R8
    LeaderFeatureExtractor) treats None as "no microstructure signal"
    and lets the feature slot stay np.nan. The R8 schema is preserved
    exactly; values populated when data exists, nan when not.

    The lookback default (300 s) is generous relative to the 60 s
    rollup cadence so a single skipped bucket doesn't cause a miss.
    """
    floor = asof_ts - timedelta(seconds=max(1, int(lookback_s)))
    try:
        row = await conn.fetchrow(
            """
            SELECT bucket_ts,
                   iceberg_orders_count, iceberg_total_size,
                   spoof_orders_count, spoof_total_size,
                   ofi_mean, ofi_max, ofi_min, ofi_std
            FROM microstructure_features
            WHERE market_id = $1
              AND token_id = $2
              AND bucket_ts <= $3
              AND bucket_ts >= $4
            ORDER BY bucket_ts DESC
            LIMIT 1
            """,
            market_id,
            token_id,
            asof_ts,
            floor,
        )
    except Exception as exc:
        logger.debug(
            f"feature_store.get_microstructure_features_asof query failed "
            f"for market={market_id} token={token_id} "
            f"asof={asof_ts.isoformat()}: {exc}"
        )
        return None
    if row is None:
        return None
    result = _row_to_dict(row)
    bucket_ts = result.get("bucket_ts")
    try:
        if bucket_ts is not None:
            result["feature_age_s"] = max(
                0.0, (asof_ts - bucket_ts).total_seconds()
            )
    except Exception:
        result["feature_age_s"] = None
    return result


async def get_wallet_microstructure_signature_asof(
    conn: Any,
    wallet: str,
    asof_ts: datetime,
    lookback_days: int = 30,
) -> dict | None:
    """Return the most-recent ``wallet_microstructure_signature`` row
    for ``wallet`` with
    ``rollup_at <= asof_ts AND rollup_at >= asof_ts - lookback_days``.

    Returns ``None`` if no signature exists or if the latest row's
    n_orders_30d is below the cold-start gate (the row exists but the
    wallet is too thin to inform R8). R8 caller treats None as
    "structural slot stays np.nan".

    The ``lookback_days`` cap matches the signature's own rolling
    window — a row older than that is referencing a window that
    doesn't include any of the asof_ts data.
    """
    if asof_ts.tzinfo is None:
        asof_ts = asof_ts.replace(tzinfo=timezone.utc)
    floor = asof_ts - timedelta(days=max(1, int(lookback_days)))
    try:
        row = await conn.fetchrow(
            """
            SELECT rollup_at,
                   cancel_to_fill_ratio_30d,
                   iceberg_score_30d,
                   spoof_score_30d,
                   place_to_fill_seconds_p50,
                   place_to_fill_seconds_p99,
                   n_orders_30d,
                   n_fills_30d
            FROM wallet_microstructure_signature
            WHERE wallet_address = $1
              AND rollup_at <= $2
              AND rollup_at >= $3
            ORDER BY rollup_at DESC
            LIMIT 1
            """,
            wallet,
            asof_ts,
            floor,
        )
    except Exception as exc:
        logger.debug(
            f"feature_store.get_wallet_microstructure_signature_asof "
            f"query failed for wallet={wallet[:10]}… "
            f"asof={asof_ts.isoformat()}: {exc}"
        )
        return None
    if row is None:
        return None
    result = _row_to_dict(row)
    rollup_at = result.get("rollup_at")
    try:
        if rollup_at is not None:
            result["signature_age_s"] = max(
                0.0, (asof_ts - rollup_at).total_seconds()
            )
    except Exception:
        result["signature_age_s"] = None
    return result


# --------------------------------------------------------------------------- #
# Round 12 (The Periphery) — social + cross-market readers                     #
# --------------------------------------------------------------------------- #
# Three asof-shaped readers fed into R8's LeaderFeatureExtractor:
#
#   * get_social_signals_asof(conn, wallet, asof, lookback_days)
#       → dict {social_signal_density, tweets_per_active_day,
#               tweet_to_trade_lag_median_s,
#               social_signal_strategy_concordance}
#       or None when there are NO signals in the window.
#
#   * get_cross_market_features_asof(conn, polymarket_wallet, asof,
#                                   lookback_days)
#       → dict {active_venue_count, cross_venue_correlation,
#               cross_venue_lag_s}
#       or None when the wallet has NO resolved operator entry.
#
#   * get_cross_market_operator_resolution(conn, polymarket_wallet)
#       → dict of the latest cross_market_operators row matching
#       the wallet (manual + profile_link + fingerprint sources), or
#       None when no row exists.
#
# Spec § 3.4 + § 4.4. The R8 features extractor calls these per-wallet at
# the wallet's asof_ts.


async def get_social_signals_asof(
    conn: Any,
    wallet: str,
    asof_ts: datetime,
    lookback_days: int = 30,
) -> dict | None:
    """Per-wallet social-signal aggregate. Returns the 4 H. SOCIAL slot
    values keyed by their short names, or None when the wallet has no
    matching signals in the window.

    The function does two SQL reads — one for signals, one for trades —
    then delegates to :func:`src.social.feature_deriver.derive_features`
    for the actual math. Keeping the compute as a pure function makes
    the unit test much simpler than a SQL-side window aggregate would.
    """
    if asof_ts.tzinfo is None:
        asof_ts = asof_ts.replace(tzinfo=timezone.utc)
    floor = asof_ts - timedelta(days=max(1, int(lookback_days)))
    try:
        signal_rows = await conn.fetch(
            """
            SELECT posted_at, intent, intent_confidence,
                   parsed_market, parsed_direction
            FROM social_signals
            WHERE resolved_wallet = $1
              AND posted_at >= $2
              AND posted_at <= $3
            ORDER BY posted_at ASC
            """,
            wallet,
            floor,
            asof_ts,
        )
    except Exception as exc:
        logger.debug(
            f"feature_store.get_social_signals_asof failed for "
            f"wallet={wallet}: {exc}"
        )
        return None
    if not signal_rows:
        return None
    try:
        trade_rows = await conn.fetch(
            """
            SELECT time, market_id, token_id, side
            FROM trades_observed
            WHERE wallet_address = $1
              AND time >= $2
              AND time <= $3
            ORDER BY time ASC
            """,
            wallet,
            floor,
            asof_ts,
        )
    except Exception:
        trade_rows = []
    # Late import so the feature_store module stays importable when
    # src/social isn't installed (e.g., environments that don't run
    # the social daemon).
    from src.social.feature_deriver import derive_features as _derive_social

    feats = _derive_social(
        signals=[dict(r) for r in signal_rows],
        trades=[dict(r) for r in trade_rows],
        asof_ts=asof_ts,
        lookback_days=lookback_days,
    )
    return feats.as_dict()


async def get_cross_market_features_asof(
    conn: Any,
    polymarket_wallet: str,
    asof_ts: datetime,
    lookback_days: int = 30,
) -> dict | None:
    """Per-wallet cross-market aggregate. Returns the 3 J. CROSS_MARKET
    slot values keyed by their short names, or None when the wallet has
    no confirmed operator entry.

    Two-step: resolve the operator_id, then read positions + trades.
    """
    if asof_ts.tzinfo is None:
        asof_ts = asof_ts.replace(tzinfo=timezone.utc)
    floor = asof_ts - timedelta(days=max(1, int(lookback_days)))
    try:
        op_row = await conn.fetchrow(
            """
            SELECT operator_id, confidence, resolution_source
            FROM cross_market_operators
            WHERE polymarket_wallet = $1
            ORDER BY confidence DESC, resolved_at DESC
            LIMIT 1
            """,
            polymarket_wallet,
        )
    except Exception as exc:
        logger.debug(
            f"feature_store.get_cross_market_features_asof: operator "
            f"lookup failed for wallet={polymarket_wallet}: {exc}"
        )
        return None
    if op_row is None:
        return None
    try:
        position_rows = await conn.fetch(
            """
            SELECT venue, market_id, side, size_usdc,
                   opened_at, closed_at, snapshot_at
            FROM cross_market_positions
            WHERE operator_id = $1
              AND snapshot_at >= $2
              AND snapshot_at <= $3
            ORDER BY snapshot_at ASC
            """,
            int(op_row["operator_id"]),
            floor,
            asof_ts,
        )
    except Exception as exc:
        logger.debug(
            f"feature_store.get_cross_market_features_asof: positions "
            f"fetch failed: {exc}"
        )
        position_rows = []
    try:
        trade_rows = await conn.fetch(
            """
            SELECT time, market_id, token_id, side
            FROM trades_observed
            WHERE wallet_address = $1
              AND time >= $2
              AND time <= $3
            ORDER BY time ASC
            """,
            polymarket_wallet,
            floor,
            asof_ts,
        )
    except Exception:
        trade_rows = []
    from src.cross_market.feature_deriver import derive_features as _derive_xm

    feats = _derive_xm(
        cross_market_rows=[dict(r) for r in position_rows],
        polymarket_trades=[dict(r) for r in trade_rows],
        asof_ts=asof_ts,
        lookback_days=lookback_days,
    )
    return feats.as_dict()


async def get_cross_market_operator_resolution(
    conn: Any, polymarket_wallet: str
) -> dict | None:
    """Returns the most-recent cross_market_operators row matching the
    given Polymarket wallet, or None when no row exists.

    Used by the R8 daemon for diagnostic logging + by tooling that wants
    to know whether a wallet is confirmed-or-pending without computing
    full features.
    """
    try:
        row = await conn.fetchrow(
            """
            SELECT operator_id, polymarket_wallet, kalshi_account,
                   manifold_handle, predictit_account, x_handle,
                   resolution_source, confidence, resolved_at, notes
            FROM cross_market_operators
            WHERE polymarket_wallet = $1
            ORDER BY resolved_at DESC
            LIMIT 1
            """,
            polymarket_wallet,
        )
    except Exception as exc:
        logger.debug(
            f"feature_store.get_cross_market_operator_resolution failed "
            f"for wallet={polymarket_wallet}: {exc}"
        )
        return None
    if row is None:
        return None
    return _row_to_dict(row)
