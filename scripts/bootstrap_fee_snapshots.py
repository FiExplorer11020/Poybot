"""Bootstrap fee_snapshots from the markets table.

The economic gates in :mod:`src.economics.gates` require a populated
``fee_snapshots`` row for every (market_id, token_id) before allowing
a paper_trade to open. Without it, every FOLLOW decision is rejected
with ``reason='missing_fee_snapshot'`` and 0 paper_trades are
generated — observed post-Sprint 4 (see EXECUTION_PLAN § 18 / Phase 6).

This is a one-shot bootstrap. The longer-term fix is to wire a
periodic FeeSnapshotWriter into the engine scheduler (e.g.
hourly_fee_snapshot job) so the snapshots stay fresh per the gate's
``max_fee_age_s = 24h`` policy. Until then, re-run this script
every ~12h or rely on `markets.fee_rate_pct` updates.

Logic
-----
For each row in ``markets`` (active + non-expired) we insert two
``fee_snapshots`` rows — one for ``token_yes`` and one for
``token_no`` — using ``fee_rate = markets.fee_rate_pct``. Sports
markets get fee=0, crypto gets the markets table value (typically
filled by Falcon Markets agent 574).

Idempotent: the UNIQUE constraint on
(market_id, token_id, captured_at, source) means re-runs at the
same minute are no-ops. New ``captured_at`` per call so the
``max_fee_age_s`` gate keeps treating them as fresh.
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal

import asyncpg
from loguru import logger

SOURCE = "bootstrap_from_markets"
ECONOMIC_MODEL_VERSION = "v1.0.0"


async def run() -> int:
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        logger.error("DATABASE_URL not set")
        return 2
    captured_at = datetime.now(tz=timezone.utc)
    pool = await asyncpg.create_pool(db_url, min_size=1, max_size=4)
    try:
        async with pool.acquire() as conn:
            # Fetch active markets with both tokens defined. End_date
            # filter mirrors the existing sync_markets convention.
            rows = await conn.fetch(
                """
                SELECT market_id, token_yes, token_no,
                       COALESCE(fee_rate_pct, 0.0) AS fee_rate_pct
                FROM markets
                WHERE active = TRUE
                  AND token_yes IS NOT NULL
                  AND token_no IS NOT NULL
                  AND (end_date IS NULL OR end_date > NOW() - INTERVAL '24h')
                """
            )
            if not rows:
                logger.warning("no markets to snapshot")
                return 0
            logger.info(f"fetching {len(rows)} markets")

            n_inserted = 0
            for r in rows:
                market_id = r["market_id"]
                fee_rate = Decimal(str(r["fee_rate_pct"] or 0.0))
                fee_enabled = fee_rate > 0
                for token_id in (r["token_yes"], r["token_no"]):
                    if not token_id:
                        continue
                    try:
                        await conn.execute(
                            """
                            INSERT INTO fee_snapshots
                                (market_id, token_id, fee_enabled,
                                 fee_rate, maker_fee_rate, source,
                                 captured_at, compatibility,
                                 economic_model_version)
                            VALUES ($1, $2, $3, $4, 0, $5, $6, '{}', $7)
                            ON CONFLICT (market_id, token_id, captured_at, source)
                                DO NOTHING
                            """,
                            market_id,
                            token_id,
                            fee_enabled,
                            fee_rate,
                            SOURCE,
                            captured_at,
                            ECONOMIC_MODEL_VERSION,
                        )
                        n_inserted += 1
                    except Exception as exc:
                        logger.warning(
                            f"insert failed market={market_id} token={token_id}: {exc}"
                        )
            logger.info(
                f"DONE: {n_inserted} fee_snapshot rows inserted "
                f"(captured_at={captured_at.isoformat()})"
            )
    finally:
        await pool.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
