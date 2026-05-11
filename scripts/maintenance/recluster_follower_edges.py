"""
One-shot operator command — recompute every confirmed `follower_edges`
row using the Phase 3 Round 2 BIVARIATE Hawkes fitter.

Why it matters
--------------
Up to and including Phase 3 Round 1, `hawkes_alpha_mu` on every
`follower_edges` row was the output of a UNIVARIATE self-exciting fit
on the follower's own trade times. That measures burstiness, not
leader→follower causality. Round 2 swapped the model to a true
bivariate fit, but only NEW writes use the corrected semantics. This
script back-fills the old rows so the "α/μ > 1 → confirmed" gate finally
reflects causality on the whole table.

How it runs
-----------
Reads every `follower_edges` row, pulls 30 days of trade timestamps for
both the leader and the follower (from `trades_observed`), invokes
`HawkesFitter.fit_arrays`, writes the new columns
(hawkes_alpha, hawkes_mu, hawkes_beta, hawkes_log_likelihood,
 hawkes_n_leader_events, hawkes_fit_at, hawkes_alpha_mu).

Operator action
---------------
This is the recommended catch-up after deploying Phase 3 Round 2:

    python scripts/maintenance/recluster_follower_edges.py --confirm

By default (no flag) the script runs in DRY-RUN mode and just prints
expected counts. Pass --confirm to actually write. Some edges that
previously α/μ > 1 via self-excitation will rightly drop below 1 on
the bivariate fit — that's the expected, desired outcome.

Cron-friendly
-------------
After the one-shot run, the nightly batch job in `src/engine/jobs/`
keeps every row fresh going forward. This script is not intended to
run on a schedule.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timedelta, timezone

import asyncpg
from loguru import logger

from src.config import settings
from src.database.connection import get_db


LOOKBACK_DAYS = 30


async def _list_edges(conn: asyncpg.Connection) -> list[asyncpg.Record]:
    return await conn.fetch(
        """
        SELECT leader_wallet, follower_wallet
        FROM follower_edges
        WHERE co_occurrences >= 5
        ORDER BY co_occurrences DESC
        """
    )


async def _trade_times(
    conn: asyncpg.Connection, wallet: str, since: datetime
) -> list[float]:
    rows = await conn.fetch(
        """
        SELECT EXTRACT(EPOCH FROM time) AS ts
        FROM trades_observed
        WHERE wallet_address = $1 AND time >= $2
        ORDER BY time ASC
        """,
        wallet,
        since,
    )
    return [float(r["ts"]) for r in rows]


async def _write_fit(
    conn: asyncpg.Connection,
    leader: str,
    follower: str,
    result: dict,
) -> None:
    await conn.execute(
        """
        UPDATE follower_edges
        SET
            hawkes_alpha          = $3,
            hawkes_mu             = $4,
            hawkes_beta           = $5,
            hawkes_log_likelihood = $6,
            hawkes_n_leader_events = $7,
            hawkes_alpha_mu       = $8,
            hawkes_fit_at         = NOW()
        WHERE leader_wallet = $1 AND follower_wallet = $2
        """,
        leader,
        follower,
        result["alpha"],
        result["mu"],
        result["beta"],
        result.get("log_likelihood"),
        result.get("n_leader_events", 0),
        result["alpha_mu_ratio"],
    )


async def _run(confirm: bool) -> int:
    # Late import — keeps `--help` snappy for operators.
    from src.graph.hawkes_fitter import HawkesFitter

    fitter = HawkesFitter()
    since = datetime.now(tz=timezone.utc) - timedelta(days=LOOKBACK_DAYS)

    async with get_db() as conn:
        edges = await _list_edges(conn)
        logger.info(f"recluster: {len(edges)} edges with co_occurrences >= 5")

        if not confirm:
            logger.warning(
                "DRY-RUN mode (no writes). Re-run with --confirm to update "
                f"the {len(edges)} rows. Each fit takes ~10-50ms; total "
                f"~{len(edges) * 0.03:.0f}s wall."
            )
            return 0

        written = 0
        for i, edge in enumerate(edges, 1):
            leader = edge["leader_wallet"]
            follower = edge["follower_wallet"]
            leader_times = await _trade_times(conn, leader, since)
            follower_times = await _trade_times(conn, follower, since)
            if len(leader_times) < 5 or len(follower_times) < 5:
                logger.debug(
                    f"skip {leader[:8]}→{follower[:8]}: "
                    f"insufficient data ({len(leader_times)} L, "
                    f"{len(follower_times)} F)"
                )
                continue
            try:
                result = fitter.fit_arrays(leader_times, follower_times)
            except Exception as exc:  # pragma: no cover
                logger.warning(
                    f"fit failed for {leader[:8]}→{follower[:8]}: {exc}"
                )
                continue
            if result is None:
                continue
            await _write_fit(conn, leader, follower, result)
            written += 1
            if i % 100 == 0:
                logger.info(f"  progress: {i}/{len(edges)} ({written} written)")

        logger.info(f"recluster done: {written}/{len(edges)} rows updated")
        return written


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Actually write. Without this flag the script is a dry-run.",
    )
    args = parser.parse_args()
    written = asyncio.run(_run(confirm=args.confirm))
    return 0 if written >= 0 else 1


if __name__ == "__main__":
    sys.exit(main())
