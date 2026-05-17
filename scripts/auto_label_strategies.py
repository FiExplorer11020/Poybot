"""Auto-labeller for strategy_classifier — Sprint 2 Day 3.2.

Derives weak labels (labeller='auto_v1', confidence=0.5) from behavioral
signals in positions_reconstructed + trades_observed. Inserts into
strategy_labels so the LightGBM trainer has a dataset to fit on.

Rules (cascade, most specific first):

* **structural_bot** — trades_per_day >= 100 AND median_holding_s < 60
* **market_maker** — trades_per_day >= 20 AND median_holding_s < 300
* **arb_2way** — wallet has BOTH YES and NO positions in the same market
                  during the window (paired direction observation)
* **directional** — median_holding_s >= 86_400 (>= 24h)
* **momentum** — median_holding_s < 3_600 (< 1h) AND trades_per_day >= 2

Wallets that match none of these rules are not labelled (they get a
"skip" — the operator can hand-label later via the notebook).

Idempotent at the row level: we INSERT with labeller='auto_v1', which is
treated as a distinct labeller from human ops. Re-running produces an
additional row per (wallet, window); ``StrategyLabelStore.get_labelled_set_for_training``
picks the latest via DISTINCT ON, so duplicates don't break training.

Run example
-----------

.. code-block:: bash

    docker exec polymarket_observer python -m scripts.auto_label_strategies \\
        --min-positions 3 --lookback-days 30
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import date, datetime, timedelta, timezone

import asyncpg
from loguru import logger

LABELLER = "auto_v2"
LABEL_CONFIDENCE = 0.5
# v2 widens the directional threshold from 24h → 12h. After the
# Sprint 1 backfill (517k historical trades), most "swing" leaders
# show median holding in the 12-48h range. The 24h cutoff missed all
# of them — auto_v1 produced 0 directional labels out of 60. v2 also
# filters out positions with close_time < open_time (the data-quality
# bug noted in EXECUTION_PLAN § 14 dev #5).
DIRECTIONAL_HOLDING_S_MIN = 43_200  # 12h

# Canonical 9 strategy classes — must match the DB CHECK constraint.
STRATEGY_CLASSES = (
    "directional",
    "momentum",
    "contrarian",
    "arb_2way",
    "arb_3way",
    "market_maker",
    "structural_bot",
    "info_leak",
    "social_driven",
)


async def fetch_candidates(
    conn: asyncpg.Connection,
    min_positions: int,
    lookback_days: int,
) -> list[asyncpg.Record]:
    """Wallets with >= min_positions closed positions in window + per-wallet
    holding-period summary stats. One DB roundtrip.
    """
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=lookback_days)
    rows = await conn.fetch(
        """
        SELECT
            wallet_address,
            COUNT(*)::int AS n_positions,
            COUNT(DISTINCT market_id)::int AS distinct_markets,
            (percentile_cont(0.5) WITHIN GROUP (
                ORDER BY EXTRACT(EPOCH FROM (close_time - open_time))
            ))::float AS median_holding_s,
            AVG(EXTRACT(EPOCH FROM (close_time - open_time)))::float AS mean_holding_s,
            SUM(CASE WHEN close_method = 'merge' THEN 1 ELSE 0 END)::float
                / NULLIF(COUNT(*), 0) AS merge_pct,
            SUM(CASE WHEN close_method = 'resolution' THEN 1 ELSE 0 END)::float
                / NULLIF(COUNT(*), 0) AS resolution_pct
        FROM positions_reconstructed
        WHERE close_time IS NOT NULL
          AND open_time >= $1
          -- v2: filter out the data-quality bug from
          -- `position_tracker._close_position` where merge/resolution
          -- close races produce close_time < open_time (skews the
          -- holding-period median negative).
          AND close_time > open_time
        GROUP BY wallet_address
        HAVING COUNT(*) >= $2
        ORDER BY COUNT(*) DESC
        """,
        cutoff,
        min_positions,
    )
    return list(rows)


async def fetch_arb_pair_flags(
    conn: asyncpg.Connection,
    wallets: list[str],
    lookback_days: int,
) -> dict[str, bool]:
    """Bulk: which wallets have at least one market with BOTH 'yes' and 'no'
    direction positions in the window? Single roundtrip via ANY($1)."""
    if not wallets:
        return {}
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=lookback_days)
    rows = await conn.fetch(
        """
        SELECT DISTINCT wallet_address
        FROM (
            SELECT wallet_address, market_id
            FROM positions_reconstructed
            WHERE wallet_address = ANY($1)
              AND open_time >= $2
            GROUP BY wallet_address, market_id
            HAVING COUNT(DISTINCT direction) >= 2
        ) x
        """,
        wallets,
        cutoff,
    )
    paired = {r["wallet_address"] for r in rows}
    return {w: (w in paired) for w in wallets}


async def fetch_trade_frequencies(
    conn: asyncpg.Connection,
    wallets: list[str],
    lookback_days: int,
) -> dict[str, float]:
    """Bulk: trades_per_day for each wallet (n_trades / n_active_days)."""
    if not wallets:
        return {}
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=lookback_days)
    rows = await conn.fetch(
        """
        SELECT
            wallet_address,
            COUNT(*)::int AS n_trades,
            COUNT(DISTINCT DATE(time))::int AS active_days
        FROM trades_observed
        WHERE wallet_address = ANY($1)
          AND time >= $2
          AND source IS DISTINCT FROM 'onchain'
        GROUP BY wallet_address
        """,
        wallets,
        cutoff,
    )
    out: dict[str, float] = {w: 0.0 for w in wallets}
    for r in rows:
        days = r["active_days"] or 0
        if days > 0:
            out[r["wallet_address"]] = float(r["n_trades"]) / float(days)
    return out


def classify(
    cand: asyncpg.Record,
    trades_per_day: float,
    has_arb_pair: bool,
) -> tuple[str | None, str | None]:
    """Apply rule cascade. Returns (strategy, rationale) or (None, None)."""
    holding_s = float(cand["median_holding_s"] or 0.0)
    n_pos = int(cand["n_positions"] or 0)

    # Rule 1: structural_bot (very fast trades + very high freq).
    if trades_per_day >= 100 and holding_s < 60:
        return (
            "structural_bot",
            f"trades_per_day={trades_per_day:.1f}>=100 + median_holding_s={holding_s:.1f}<60",
        )

    # Rule 2: market_maker (short hold + high freq).
    if trades_per_day >= 20 and holding_s < 300:
        return (
            "market_maker",
            f"trades_per_day={trades_per_day:.1f}>=20 + median_holding_s={holding_s:.1f}<300",
        )

    # Rule 3: directional (long hold) — v2 promoted to fire BEFORE
    # arb_2way. Rationale: a wallet that holds positions median >= 12h
    # is by definition NOT pure arbitrage (true arb_2way exits within
    # minutes once prices converge). The previous order treated any
    # paired YES+NO observation as arb_2way which swallowed swing
    # traders that incidentally entered both legs of a market.
    if holding_s >= DIRECTIONAL_HOLDING_S_MIN:
        return (
            "directional",
            f"median_holding_s={holding_s:.0f}s>={DIRECTIONAL_HOLDING_S_MIN} (12h)",
        )

    # Rule 4: arb_2way (paired YES+NO in same market, short hold).
    if has_arb_pair:
        return (
            "arb_2way",
            f"paired_yes_no_observed (n_positions={n_pos}, "
            f"median_holding_s={holding_s:.0f})",
        )

    # Rule 5: momentum (short hold + regular cadence).
    if holding_s < 3_600 and trades_per_day >= 2:
        return (
            "momentum",
            f"median_holding_s={holding_s:.0f}s<3600 + trades_per_day={trades_per_day:.1f}>=2",
        )

    return None, None


async def insert_label(
    conn: asyncpg.Connection,
    wallet: str,
    window_start: date,
    window_end: date,
    strategy: str,
    rationale: str,
) -> bool:
    """Direct INSERT, mirroring StrategyLabelStore.insert_label. Returns True
    on success, False on (caught) error."""
    if strategy not in STRATEGY_CLASSES:
        logger.warning(f"skipping {wallet}: invalid strategy {strategy!r}")
        return False
    try:
        await conn.execute(
            """
            INSERT INTO strategy_labels
                (wallet_address, label_window_start, label_window_end,
                 primary_strategy, secondary_strategy, confidence,
                 labeller, labelled_at, rationale)
            VALUES ($1, $2, $3, $4, NULL, $5, $6, NOW(), $7)
            """,
            wallet,
            window_start,
            window_end,
            strategy,
            LABEL_CONFIDENCE,
            LABELLER,
            rationale,
        )
        return True
    except Exception as exc:
        logger.warning(f"insert failed for {wallet}: {exc}")
        return False


async def run(args: argparse.Namespace) -> int:
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        logger.error("DATABASE_URL not set")
        return 2

    pool = await asyncpg.create_pool(db_url, min_size=1, max_size=4)
    try:
        async with pool.acquire() as conn:
            candidates = await fetch_candidates(
                conn, args.min_positions, args.lookback_days
            )
            logger.info(
                f"Got {len(candidates)} candidate wallets "
                f"(min_positions={args.min_positions}, "
                f"lookback_days={args.lookback_days})"
            )
            if not candidates:
                logger.warning("no candidates — nothing to label")
                return 0

            wallets = [r["wallet_address"] for r in candidates]
            arb_flags = await fetch_arb_pair_flags(conn, wallets, args.lookback_days)
            trade_freqs = await fetch_trade_frequencies(
                conn, wallets, args.lookback_days
            )

            window_end = date.today()
            window_start = window_end - timedelta(days=args.lookback_days)

            inserted = 0
            no_match = 0
            distribution: dict[str, int] = {}
            for cand in candidates:
                wallet = cand["wallet_address"]
                strategy, rationale = classify(
                    cand,
                    trade_freqs.get(wallet, 0.0),
                    arb_flags.get(wallet, False),
                )
                if strategy is None:
                    no_match += 1
                    continue
                ok = await insert_label(
                    conn, wallet, window_start, window_end, strategy, rationale
                )
                if ok:
                    inserted += 1
                    distribution[strategy] = distribution.get(strategy, 0) + 1

            logger.info(
                f"DONE: {inserted} labels inserted, "
                f"{no_match} wallets unmatched, "
                f"distribution={distribution}"
            )
    finally:
        await pool.close()
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--min-positions",
        type=int,
        default=3,
        help="Minimum closed positions in window for a wallet to be labelled.",
    )
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=90,
        help=(
            "Window length for the holding-period + frequency stats. "
            "v2 default is 90 days (was 30 in v1) so the backfilled "
            "historical trades are within the lookback window — this "
            "is the main reason v2 produces ~3-5x more labels than v1."
        ),
    )
    args = parser.parse_args()
    sys.exit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
