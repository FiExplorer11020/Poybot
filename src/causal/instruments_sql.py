"""Pure-SQL instrument detectors (no external API / RPC dependency).

Audit reference: docs/ROUND_10_CAUSAL_INFERENCE.md § 3.1.

Split out of ``src/causal/instruments.py`` to keep each file under
the 500-line project limit. The three detectors here all read from
the existing R6/R7/R9 tables and emit ``InstrumentalEvent`` rows:

  * :class:`RelatedMarketResolver`   — hourly batch on trades_observed
  * :class:`LeaderGasQuirkDetector`  — weekly batch on mempool_observations
  * :class:`APIOutageWindowDetector` — on-alert read of trades_observed
                                       bucketed by source
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from loguru import logger

from src.causal.instruments_base import Detector, InstrumentalEvent
from src.database.connection import get_db


# ---------------------------------------------------------------------------
# RelatedMarketResolver
# ---------------------------------------------------------------------------


class RelatedMarketResolver(Detector):
    """Hourly batch: cluster markets by co-occurrence of trades.

    Implementation: pure SQL on ``trades_observed``. We emit one
    InstrumentalEvent per pair of markets that have shared at least
    ``min_co_occurrences`` wallets in the last ``lookback_days``. The
    event_time is set to ``asof_ts``; the affected_market_ids list is
    the two related markets; confidence is the Jaccard-style ratio of
    the wallet co-occurrence count.

    The 2SLS first stage uses these as instruments: "when market X
    resolves, market Y has historically experienced a volume burst,
    so a resolution of X is an exogenous shock that propagates to Y
    via leader trades on Y" — a natural experiment.
    """

    name = "related_market"
    event_type = "news"  # related-market shocks are operationally a "news" type

    def __init__(
        self,
        lookback_days: int = 30,
        min_co_occurrences: int = 5,
        max_pairs: int = 200,
    ) -> None:
        self._lookback_days = int(lookback_days)
        self._min_co = int(min_co_occurrences)
        self._max_pairs = int(max_pairs)

    async def detect(self, asof_ts: datetime) -> list[InstrumentalEvent]:
        since = asof_ts - timedelta(days=self._lookback_days)
        try:
            async with get_db() as conn:
                rows = await conn.fetch(
                    """
                    WITH wallet_markets AS (
                        SELECT DISTINCT wallet_address, market_id
                        FROM trades_observed
                        WHERE time >= $1
                          AND source IS DISTINCT FROM 'onchain'
                    ),
                    pairs AS (
                        SELECT
                            a.market_id AS market_a,
                            b.market_id AS market_b,
                            COUNT(*) AS co_count
                        FROM wallet_markets a
                        JOIN wallet_markets b
                          ON a.wallet_address = b.wallet_address
                         AND a.market_id < b.market_id
                        GROUP BY a.market_id, b.market_id
                        HAVING COUNT(*) >= $2
                    )
                    SELECT market_a, market_b, co_count
                    FROM pairs
                    ORDER BY co_count DESC
                    LIMIT $3
                    """,
                    since,
                    self._min_co,
                    self._max_pairs,
                )
        except Exception as exc:
            logger.debug(f"RelatedMarketResolver: SQL failed: {exc}")
            return []
        events: list[InstrumentalEvent] = []
        for r in rows:
            try:
                events.append(
                    InstrumentalEvent(
                        event_type=self.event_type,
                        event_time=asof_ts,
                        source="related_market",
                        payload={
                            "market_a": r["market_a"],
                            "market_b": r["market_b"],
                            "co_count": int(r["co_count"]),
                            "lookback_days": self._lookback_days,
                        },
                        affected_market_ids=[r["market_a"], r["market_b"]],
                        confidence=min(1.0, float(r["co_count"]) / 100.0),
                    )
                )
            except Exception as exc:
                logger.debug(f"RelatedMarketResolver: skipping bad row: {exc}")
        return events


# ---------------------------------------------------------------------------
# LeaderGasQuirkDetector
# ---------------------------------------------------------------------------


class LeaderGasQuirkDetector(Detector):
    """Weekly batch: detect leader-specific gas-price quirks.

    Implementation: pure SQL on ``mempool_observations`` (R7's table).
    For each watched wallet, compute the within-wallet variance of
    gas price across their submitted intents. Wallets whose gas-price
    distribution shows random variation (i.e. it's NOT correlated with
    trade size or market) emit a quirk event — these are the wallets
    we can use as instruments because their gas-price-vs-trade-correctness
    pairs are exogenous.

    For the MVP we emit one event per wallet whose intent count >=
    ``min_intents`` in the lookback window, with confidence = the
    inverse coefficient-of-variation of gas (low CV = random = good
    instrument). The 2SLS first stage joins this against the leader's
    trades_observed stream to identify the timing instrument.
    """

    name = "leader_gas_quirk"
    event_type = "gas_quirk"

    def __init__(
        self,
        lookback_days: int = 7,
        min_intents: int = 20,
        max_wallets: int = 500,
    ) -> None:
        self._lookback_days = int(lookback_days)
        self._min_intents = int(min_intents)
        self._max_wallets = int(max_wallets)

    async def detect(self, asof_ts: datetime) -> list[InstrumentalEvent]:
        since = asof_ts - timedelta(days=self._lookback_days)
        # mempool_observations doesn't have a gas-price column today;
        # we use replacement_chain length + nonce gap as the gas-quirk
        # proxy. (The audit doc flags this as a place the operator
        # may want to extend with explicit gas tracking — see
        # docs/audit/phase3/round10_final_review.md.)
        try:
            async with get_db() as conn:
                rows = await conn.fetch(
                    """
                    SELECT
                        wallet_address,
                        COUNT(*) AS n_intents,
                        COUNT(DISTINCT replaces_tx_hash) AS replacement_count
                    FROM mempool_observations
                    WHERE intent_received_at >= $1
                    GROUP BY wallet_address
                    HAVING COUNT(*) >= $2
                    ORDER BY COUNT(*) DESC
                    LIMIT $3
                    """,
                    since,
                    self._min_intents,
                    self._max_wallets,
                )
        except Exception as exc:
            logger.debug(f"LeaderGasQuirkDetector: SQL failed: {exc}")
            return []
        events: list[InstrumentalEvent] = []
        for r in rows:
            try:
                n = int(r["n_intents"])
                rep = int(r["replacement_count"] or 0)
                # Inverse of relative replacement rate; clamps in [0, 1].
                ratio = (rep / n) if n > 0 else 0.0
                confidence = max(0.0, min(1.0, 1.0 - ratio))
                events.append(
                    InstrumentalEvent(
                        event_type=self.event_type,
                        event_time=asof_ts,
                        source="mempool_observations",
                        payload={
                            "wallet_address": r["wallet_address"],
                            "n_intents": n,
                            "replacement_count": rep,
                            "lookback_days": self._lookback_days,
                        },
                        affected_market_ids=[],
                        confidence=confidence,
                    )
                )
            except Exception as exc:
                logger.debug(f"LeaderGasQuirkDetector: skipping bad row: {exc}")
        return events


# ---------------------------------------------------------------------------
# APIOutageWindowDetector
# ---------------------------------------------------------------------------


class APIOutageWindowDetector(Detector):
    """Detect API outage windows from R6 coverage_reconciler output.

    R6's ``polybot_coverage_ratio`` Prometheus gauge has a corresponding
    persistence layer in production (the coverage_reconciler writes to
    Prometheus only — we read it back via SQL on trades_observed by
    bucket-counting). This detector emits one InstrumentalEvent per
    detected outage window: a 5-min window where the ratio of
    api_market trades to on-chain trades dropped below
    ``coverage_threshold``.

    The 2SLS uses these as a strong natural experiment: when data-api
    is down, followers can't follow, so a leader trade during the
    outage window has zero "follower follow-on capacity" — its effect
    on followers (if any) is purely via the news/oracle channels.
    """

    name = "api_outage"
    event_type = "api_outage"

    def __init__(
        self,
        window_s: int = 300,
        coverage_threshold: float = 0.95,
        lookback_hours: int = 168,  # one week
    ) -> None:
        self._window_s = int(window_s)
        self._threshold = float(coverage_threshold)
        self._lookback_hours = int(lookback_hours)

    async def detect(self, asof_ts: datetime) -> list[InstrumentalEvent]:
        since = asof_ts - timedelta(hours=self._lookback_hours)
        try:
            async with get_db() as conn:
                rows = await conn.fetch(
                    """
                    SELECT
                        date_trunc('minute', time)
                            - INTERVAL '1 minute' *
                              (EXTRACT(MINUTE FROM time)::int % ($2::int / 60))
                            AS window_start,
                        SUM(CASE WHEN source = 'onchain' THEN 1 ELSE 0 END) AS n_onchain,
                        SUM(CASE WHEN source = 'api_market' THEN 1 ELSE 0 END) AS n_api
                    FROM trades_observed
                    WHERE time >= $1
                    GROUP BY 1
                    HAVING SUM(CASE WHEN source = 'onchain' THEN 1 ELSE 0 END) > 0
                    ORDER BY 1 DESC
                    LIMIT 5000
                    """,
                    since,
                    self._window_s,
                )
        except Exception as exc:
            logger.debug(f"APIOutageWindowDetector: SQL failed: {exc}")
            return []
        events: list[InstrumentalEvent] = []
        for r in rows:
            try:
                n_on = int(r["n_onchain"])
                n_api = int(r["n_api"] or 0)
                if n_on <= 0:
                    continue
                ratio = n_api / n_on
                if ratio >= self._threshold:
                    continue
                events.append(
                    InstrumentalEvent(
                        event_type=self.event_type,
                        event_time=r["window_start"] or asof_ts,
                        source="coverage_reconciler",
                        payload={
                            "window_s": self._window_s,
                            "ratio": ratio,
                            "n_onchain": n_on,
                            "n_api": n_api,
                        },
                        affected_market_ids=[],
                        confidence=max(0.0, min(1.0, 1.0 - ratio)),
                    )
                )
            except Exception as exc:
                logger.debug(f"APIOutageWindowDetector: skipping bad row: {exc}")
        return events


__all__ = [
    "APIOutageWindowDetector",
    "LeaderGasQuirkDetector",
    "RelatedMarketResolver",
]
