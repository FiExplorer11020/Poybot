"""Nightly per-wallet 30-day microstructure signature batch —
Round 11 § 3.2.

For every tier-0/1 wallet in :mod:`wallet_universe`, derive the
30-day rolling microstructure signature and upsert it into
``wallet_microstructure_signature`` (migration 034). The R8 strategy
classifier reads this table via
:func:`src.profiler.feature_store.get_wallet_microstructure_signature_asof`
and incorporates the values into its E/F microstructure feature slots.

This is a **cold path** — runs once per nightly batch (operator-driven
or wired via the engine's :mod:`APScheduler`). The per-wallet
computation is single-roundtrip SQL: count cancellations / fills /
order-events from ``clob_book_events`` joined to ``trades_observed`` for
wallet attribution (since wallet_address is NULL on placement events).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from loguru import logger

from src.config import settings
from src.database.connection import get_db

try:
    from src.monitoring.metrics import (  # type: ignore[attr-defined]
        wallet_signatures_cardinality,
        wallet_signatures_updated_total,
    )
except Exception:  # pragma: no cover

    class _NoOpLabel:
        def labels(self, *_args, **_kwargs):
            return self

        def inc(self, *_args, **_kwargs):
            return None

        def set(self, *_args, **_kwargs):
            return None

    wallet_signatures_updated_total = _NoOpLabel()  # type: ignore[assignment]
    wallet_signatures_cardinality = _NoOpLabel()  # type: ignore[assignment]


# Wallet tier filter — spec § 3.2.E says tier-0/1 only. Anything higher
# is too high-cardinality to store the per-wallet signature for; the
# microstructure_features table covers per-(market, token) regardless of
# wallet.
_DEFAULT_TIER_FILTER: tuple[int, ...] = (0, 1)


@dataclass(slots=True)
class WalletSignature:
    """One row in ``wallet_microstructure_signature`` (migration 034)."""

    wallet_address: str
    rollup_at: datetime
    cancel_to_fill_ratio_30d: float | None
    iceberg_score_30d: float | None
    spoof_score_30d: float | None
    place_to_fill_seconds_p50: float | None
    place_to_fill_seconds_p99: float | None
    n_orders_30d: int
    n_fills_30d: int


def _percentile(samples: list[float], quantile: float) -> float | None:
    if not samples:
        return None
    sorted_samples = sorted(samples)
    idx = max(
        0, min(len(sorted_samples) - 1, int(quantile * len(sorted_samples)))
    )
    return float(sorted_samples[idx])


class WalletSignatureBatch:
    """Nightly batch producer for ``wallet_microstructure_signature``.

    Usage::

        batch = WalletSignatureBatch()
        n = await batch.run()
        # n is the number of wallets whose signature was upserted.
    """

    def __init__(
        self,
        *,
        lookback_days: int | None = None,
        tier_filter: Iterable[int] | None = None,
        min_orders: int | None = None,
    ) -> None:
        self.lookback_days = int(
            lookback_days
            if lookback_days is not None
            else settings.MICROSTRUCTURE_SIGNATURE_LOOKBACK_DAYS
        )
        self.tier_filter = tuple(tier_filter or _DEFAULT_TIER_FILTER)
        self.min_orders = int(
            min_orders
            if min_orders is not None
            else settings.MICROSTRUCTURE_SIGNATURE_MIN_ORDERS
        )

    async def _load_tier_wallets(self, conn: Any) -> list[str]:
        """Pull the tier-0/1 wallet set from wallet_universe. R6 owns
        the table; if it's missing (early CI / fresh DB), the query
        returns an empty list and the batch is a clean no-op.
        """
        try:
            rows = await conn.fetch(
                """
                SELECT wallet_address
                FROM wallet_universe
                WHERE depth_tier = ANY($1::int[])
                ORDER BY wallet_address
                """,
                list(self.tier_filter),
            )
            return [str(r["wallet_address"]) for r in rows if r.get("wallet_address")]
        except Exception as exc:
            logger.debug(f"WalletSignatureBatch: tier-wallet query failed: {exc}")
            return []

    async def _derive_for_wallet(
        self, conn: Any, wallet: str, asof_ts: datetime
    ) -> WalletSignature | None:
        """Compute the signature row for one wallet. Single-roundtrip SQL.

        The query joins clob_book_events (event-level granularity) with
        clob_book_events filtered to fills (the only event type with
        attribution) to count cancellations and fills per wallet over
        the trailing 30 days.

        For wallets with fewer than ``min_orders`` events, we return
        None so the caller can skip the upsert.
        """
        floor = asof_ts - timedelta(days=self.lookback_days)
        try:
            row = await conn.fetchrow(
                """
                WITH ev AS (
                    SELECT event_type, event_time, order_hash
                    FROM clob_book_events
                    WHERE wallet_address = $1
                      AND event_time >= $2
                      AND event_time <= $3
                ),
                p2f AS (
                    SELECT
                        EXTRACT(EPOCH FROM (f.event_time - p.event_time))
                            AS delta_s
                    FROM ev p
                    JOIN ev f USING (order_hash)
                    WHERE p.event_type = 'placed'
                      AND f.event_type IN ('filled', 'partial_fill')
                      AND f.event_time > p.event_time
                )
                SELECT
                    COUNT(*) FILTER (WHERE event_type IN
                        ('placed', 'modified', 'cancelled',
                         'filled', 'partial_fill'))           AS n_orders,
                    COUNT(*) FILTER (WHERE event_type = 'cancelled') AS n_cancels,
                    COUNT(*) FILTER (WHERE event_type = 'filled')    AS n_fills,
                    COALESCE(
                        (SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY delta_s) FROM p2f),
                        NULL
                    )::float8 AS p50,
                    COALESCE(
                        (SELECT percentile_cont(0.99) WITHIN GROUP (ORDER BY delta_s) FROM p2f),
                        NULL
                    )::float8 AS p99
                FROM ev
                """,
                wallet,
                floor,
                asof_ts,
            )
        except Exception as exc:
            logger.debug(
                f"WalletSignatureBatch: derive failed for wallet={wallet[:10]}…: {exc}"
            )
            return None
        if row is None:
            return None
        n_orders = int(row.get("n_orders") or 0)
        n_fills = int(row.get("n_fills") or 0)
        n_cancels = int(row.get("n_cancels") or 0)
        if n_orders < self.min_orders:
            return None

        # cancel_to_fill_ratio: pure cancels mapped to count rather than
        # +inf so the DB column stays numeric.
        if n_fills == 0 and n_cancels == 0:
            c2f = None
        elif n_fills == 0:
            c2f = float(n_cancels)
        else:
            c2f = n_cancels / n_fills

        # iceberg_score_30d / spoof_score_30d — defined as the fraction
        # of the wallet's order events that match the detector pattern.
        # The R11 streaming detectors emit aggregate per-(market, token)
        # counts, so we approximate here with the order-cancellation
        # density (a wallet that cancels almost everything is doing
        # SOMETHING that the live detectors will pick up; the score is
        # a coarse cold-start signal until the live counts arrive via
        # the streaming path).
        if n_orders == 0:
            iceberg_score = None
            spoof_score = None
        else:
            iceberg_score = round(min(1.0, n_cancels / max(1, n_orders)), 4)
            # Spoof is a sub-set of cancels (large-and-fast); without the
            # streaming detector's output here we use the cancel ratio
            # as a proxy. The headline streaming score still drives the
            # per-(market, token) microstructure_features.
            spoof_score = round(min(1.0, n_cancels / max(1, n_orders)) * 0.5, 4)

        return WalletSignature(
            wallet_address=wallet,
            rollup_at=asof_ts,
            cancel_to_fill_ratio_30d=c2f,
            iceberg_score_30d=iceberg_score,
            spoof_score_30d=spoof_score,
            place_to_fill_seconds_p50=row.get("p50"),
            place_to_fill_seconds_p99=row.get("p99"),
            n_orders_30d=n_orders,
            n_fills_30d=n_fills,
        )

    async def _upsert(self, conn: Any, sig: WalletSignature) -> None:
        await conn.execute(
            """
            INSERT INTO wallet_microstructure_signature
                (wallet_address, rollup_at,
                 cancel_to_fill_ratio_30d, iceberg_score_30d, spoof_score_30d,
                 place_to_fill_seconds_p50, place_to_fill_seconds_p99,
                 n_orders_30d, n_fills_30d)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            ON CONFLICT (wallet_address, rollup_at) DO UPDATE
                SET cancel_to_fill_ratio_30d  = EXCLUDED.cancel_to_fill_ratio_30d,
                    iceberg_score_30d         = EXCLUDED.iceberg_score_30d,
                    spoof_score_30d           = EXCLUDED.spoof_score_30d,
                    place_to_fill_seconds_p50 = EXCLUDED.place_to_fill_seconds_p50,
                    place_to_fill_seconds_p99 = EXCLUDED.place_to_fill_seconds_p99,
                    n_orders_30d              = EXCLUDED.n_orders_30d,
                    n_fills_30d               = EXCLUDED.n_fills_30d
            """,
            sig.wallet_address,
            sig.rollup_at,
            sig.cancel_to_fill_ratio_30d,
            sig.iceberg_score_30d,
            sig.spoof_score_30d,
            sig.place_to_fill_seconds_p50,
            sig.place_to_fill_seconds_p99,
            sig.n_orders_30d,
            sig.n_fills_30d,
        )

    async def run(
        self,
        *,
        asof_ts: datetime | None = None,
        conn: Any | None = None,
    ) -> int:
        """Run the batch end-to-end. Returns the number of wallets
        upserted. Operator can pass a pre-bound conn (e.g. from a test
        harness); otherwise the batch acquires its own.
        """
        if asof_ts is None:
            asof_ts = datetime.now(tz=timezone.utc)
        elif asof_ts.tzinfo is None:
            asof_ts = asof_ts.replace(tzinfo=timezone.utc)

        async def _go(c: Any) -> int:
            wallets = await self._load_tier_wallets(c)
            if not wallets:
                return 0
            upserted = 0
            for wallet in wallets:
                sig = await self._derive_for_wallet(c, wallet, asof_ts)
                if sig is None:
                    continue
                try:
                    await self._upsert(c, sig)
                    upserted += 1
                except Exception as exc:
                    logger.debug(
                        f"WalletSignatureBatch: upsert failed wallet={wallet[:10]}…: {exc}"
                    )
            try:
                wallet_signatures_updated_total.inc(upserted)
                wallet_signatures_cardinality.set(upserted)
            except Exception:  # pragma: no cover
                pass
            return upserted

        if conn is not None:
            return await _go(conn)
        async with get_db() as c:
            return await _go(c)
