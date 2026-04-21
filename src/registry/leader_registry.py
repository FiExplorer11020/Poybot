"""
LeaderRegistry: maintains the leaders table and refreshes it on a schedule.

Pulls top-N wallets from Falcon agent 584, enriches each with agent 581 (Wallet 360),
classifies by strategy/influence/horizon/copiability, and persists to PostgreSQL.
"""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import Any

import aiohttp
from loguru import logger

from src.config import settings
from src.registry.falcon_client import FalconAPIError, FalconClient
from src.registry.models import Leader, LeaderClassification


class LeaderRegistry:
    def __init__(self, falcon_client: FalconClient, redis_client: Any = None):
        self.falcon = falcon_client
        self.redis = redis_client
        self._stop = asyncio.Event()

    async def refresh_leaderboard(self, conn: Any) -> int:
        preserve_existing_score = False
        try:
            entries = await self.falcon.get_leaderboard(limit=settings.INITIAL_LEADER_COUNT)
        except FalconAPIError as e:
            logger.warning(f"Falcon leaderboard unavailable ({e}), falling back to PnL leaderboard")
            entries = await self._fallback_leaderboard_entries()
            preserve_existing_score = True
        if not entries:
            if preserve_existing_score:
                logger.warning("PnL leaderboard fallback returned 0 entries")
            else:
                logger.warning("Falcon leaderboard returned 0 entries, trying PnL fallback")
                entries = await self._fallback_leaderboard_entries()
                preserve_existing_score = True
            if not entries:
                cached_count = await conn.fetchval(
                    """
                    SELECT COUNT(*) FROM leaders
                    WHERE excluded = FALSE AND on_watchlist = TRUE
                    """
                )
                logger.warning(
                    "No leaderboard entries available from Falcon or fallback; "
                    f"using {int(cached_count or 0)} cached DB leaders"
                )
                return int(cached_count or 0)

        rows = [
            (e.wallet_address, float(e.falcon_score))
            for e in entries
            if e.falcon_score >= settings.MIN_FALCON_SCORE
        ]
        if not rows:
            logger.warning("Leaderboard refresh produced 0 rows after filtering")
            return 0
        await conn.executemany(
            self._leader_upsert_sql(preserve_existing_score=preserve_existing_score),
            rows,
        )
        selected_wallets = [wallet for wallet, _ in rows]
        if selected_wallets:
            await conn.execute(
                """
                UPDATE leaders
                SET on_watchlist = FALSE
                WHERE on_watchlist = TRUE
                  AND NOT (wallet_address = ANY($1::text[]))
                """,
                selected_wallets,
            )
        source = "PnL fallback" if preserve_existing_score else "Falcon"
        logger.info(f"Leaderboard refresh ({source}): upserted {len(rows)} leaders")
        return len(rows)

    async def _fallback_leaderboard_entries(self) -> list[Leader]:
        try:
            pnl_entries = await self.falcon.get_pnl_leaderboard(limit=settings.INITIAL_LEADER_COUNT)
        except FalconAPIError as e:
            logger.warning(f"PnL leaderboard fallback unavailable ({e})")
            return []
        return [
            Leader(
                wallet_address=entry.wallet_address,
                falcon_score=0.0,
            )
            for entry in pnl_entries
        ]

    @staticmethod
    def _leader_upsert_sql(*, preserve_existing_score: bool) -> str:
        if preserve_existing_score:
            return """
            INSERT INTO leaders (wallet_address, falcon_score)
            VALUES ($1, $2)
            ON CONFLICT (wallet_address)
            DO UPDATE SET
                falcon_score = leaders.falcon_score,
                on_watchlist = TRUE
            """
        return """
        INSERT INTO leaders (wallet_address, falcon_score)
        VALUES ($1, $2)
        ON CONFLICT (wallet_address)
        DO UPDATE SET
            falcon_score = EXCLUDED.falcon_score,
            on_watchlist = TRUE
        """

    async def enrich_leaders(self, conn: Any) -> int:
        # Quick probe — use a lightweight agent call that validates auth/connectivity
        try:
            await self.falcon.query(
                581,
                {"proxy_wallet": "0xabc", "window_days": "7"},
                limit=1,
            )
        except FalconAPIError:
            logger.warning("Falcon unavailable — skipping enrichment")
            return 0

        stale_cutoff = datetime.now(tz=timezone.utc) - timedelta(hours=24)
        rows = await conn.fetch(
            """
            SELECT wallet_address FROM leaders
            WHERE (last_refresh IS NULL OR last_refresh < $1)
              AND excluded = FALSE
            LIMIT $2
            """,
            stale_cutoff,
            settings.INITIAL_LEADER_COUNT,
        )
        count = 0
        for row in rows:
            wallet = row["wallet_address"]
            try:
                metrics = await self.falcon.get_wallet360(wallet)
            except FalconAPIError as e:
                logger.debug(f"Wallet360 unavailable for {wallet}: {e}")
                metrics = None
            if metrics is None:
                continue
            w360 = metrics.to_dict()
            classification = self.classify_leader(w360)
            await conn.execute(
                """
                UPDATE leaders
                SET wallet360_json = $2,
                    classification_json = $3,
                    excluded = $4,
                    exclude_reason = $5,
                    last_refresh = NOW()
                WHERE wallet_address = $1
                """,
                wallet,
                json.dumps(w360),
                json.dumps(classification.model_dump()),
                not classification.copiable,
                "structural_bot" if classification.strategy == "structural" else None,
            )
            count += 1
        logger.info(f"Enriched {count} leaders")
        return count

    def classify_leader(self, wallet360: dict) -> LeaderClassification:
        total_trades = float(wallet360.get("total_trades", 0) or 0)
        days_active = max(float(wallet360.get("days_active", 0) or 0), 1.0)
        trades_per_day = total_trades / days_active if total_trades > 0 else 0.0
        avg_duration_s = float(wallet360.get("avg_trade_duration_s", 0) or 0)
        avg_holding_days = float(wallet360.get("avg_holding_period_days", 0) or 0)
        total_volume = float(
            wallet360.get("total_volume_usdc")
            or wallet360.get("total_invested")
            or (
                float(wallet360.get("avg_position_size", 0) or 0)
                * float(wallet360.get("total_trades", 0) or 0)
            )
            or 0
        )
        falcon_score = float(
            wallet360.get("falcon_score")
            or wallet360.get("h_score")
            or wallet360.get("statistical_confidence")
            or 0
        )
        sybil_risk = bool(wallet360.get("sybil_risk_flag", False))
        timing_anomaly = bool(wallet360.get("timing_anomaly_flag", False))
        suspicious_win_rate = bool(wallet360.get("suspicious_win_rate_flag", False))
        perfect_timing = bool(wallet360.get("perfect_timing_flag", False))
        timing_corr = float(wallet360.get("trade_timing_correlation_max", 0) or 0)
        market_concentration = float(wallet360.get("market_concentration_ratio", 0) or 0)
        markets_traded = float(
            wallet360.get("markets_traded") or wallet360.get("num_markets_traded") or 0
        )
        total_pnl = float(wallet360.get("total_pnl", 0) or 0)
        risk_level = str(wallet360.get("risk_level", "") or "").upper()

        if avg_duration_s <= 0 and trades_per_day > 0:
            avg_duration_s = max(30.0, 86_400.0 / max(trades_per_day, 1.0))
        if avg_duration_s <= 0:
            avg_duration_s = 3600.0
        if avg_holding_days <= 0:
            if total_trades <= 0:
                avg_holding_days = 1.0
            elif trades_per_day >= 20:
                avg_holding_days = 1 / 48
            elif trades_per_day <= 1:
                avg_holding_days = 21
            else:
                avg_holding_days = 3

        # Strategy
        if (
            avg_duration_s < 60
            or sybil_risk
            or timing_anomaly
            or suspicious_win_rate
            or perfect_timing
            or timing_corr >= 0.98
        ):
            strategy = "structural"
        elif avg_holding_days > 14 or (
            market_concentration >= 0.65 and markets_traded <= 10 and trades_per_day <= 6
        ):
            strategy = "cognitive"
        else:
            strategy = "directional"

        # Influence
        if total_volume > 250_000 or total_pnl > 25_000:
            influence = "whale"
        elif total_volume > 25_000 or total_pnl > 2_500 or falcon_score > 5.0:
            influence = "top_trader"
        else:
            influence = "community"

        # Horizon
        if avg_holding_days < (1 / 24):  # < 1 hour
            horizon = "scalper"
        elif avg_holding_days <= 14:
            horizon = "swing"
        else:
            horizon = "holder"

        copiable = (
            strategy != "structural"
            and avg_duration_s >= 5
            and risk_level != "HIGH"
            and trades_per_day < 200
        )

        return LeaderClassification(
            strategy=strategy,
            influence=influence,
            horizon=horizon,
            copiable=copiable,
            classified_at=datetime.now(tz=timezone.utc).isoformat(),
        )

    async def sync_markets(self, conn: Any) -> int:
        """Upsert recent market metadata that is missing or stale."""
        rows = await conn.fetch(
            """
            SELECT DISTINCT market_id FROM trades_observed
            WHERE time > NOW() - INTERVAL '7 days'
              AND market_id NOT IN (
                  SELECT market_id FROM markets WHERE updated_at > NOW() - INTERVAL '24 hours'
              )
            LIMIT 100
            """
        )
        count = 0
        for row in rows:
            mid = row["market_id"]
            try:
                results = await self.falcon.query(574, {"condition_id": mid}, limit=10)
                if not results:
                    results = await self.falcon.query(574, {"market_slug": mid}, limit=10)
                m = results[0] if results else {}
            except FalconAPIError:
                m = {}
            if not m:
                m = await self._fetch_market_from_gamma(mid)

            question = m.get("question") or m.get("title") or f"Market {mid[:30]}…"
            category = m.get("category") or "unknown"
            token_yes = (
                m.get("clob_token_ids", [None])[0]
                if m.get("clob_token_ids")
                else m.get("token_yes")
            )
            token_no_list = m.get("clob_token_ids", [None, None])
            token_no = token_no_list[1] if len(token_no_list) > 1 else m.get("token_no")
            end_date_raw = m.get("end_date_iso") or m.get("endDate")
            try:
                from datetime import datetime

                end_date = (
                    datetime.fromisoformat(end_date_raw.replace("Z", "+00:00"))
                    if end_date_raw
                    else None
                )
            except Exception:
                end_date = None
            volume_24h = float(m.get("volume24hr") or m.get("volume_24h") or 0)
            liquidity = float(m.get("liquidity") or 0)
            fee_rate = float(m.get("makerBaseFee") or 0)

            try:
                await conn.execute(
                    """
                    INSERT INTO markets
                        (market_id, question, category, token_yes, token_no,
                         end_date, volume_24h, liquidity_score, fee_rate_pct, updated_at)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,NOW())
                    ON CONFLICT (market_id) DO UPDATE SET
                        question       = EXCLUDED.question,
                        category       = EXCLUDED.category,
                        token_yes      = COALESCE(EXCLUDED.token_yes, markets.token_yes),
                        token_no       = COALESCE(EXCLUDED.token_no, markets.token_no),
                        end_date       = COALESCE(EXCLUDED.end_date, markets.end_date),
                        volume_24h     = EXCLUDED.volume_24h,
                        liquidity_score= EXCLUDED.liquidity_score,
                        fee_rate_pct   = EXCLUDED.fee_rate_pct,
                        updated_at     = NOW()
                    """,
                    mid,
                    question,
                    category,
                    token_yes,
                    token_no,
                    end_date,
                    volume_24h,
                    liquidity,
                    fee_rate,
                )
                count += 1
            except Exception as exc:
                logger.debug(f"sync_markets upsert failed for {mid}: {exc}")
        if count:
            logger.info(f"sync_markets: upserted {count} markets")
        return count

    async def _fetch_market_from_gamma(self, market_id: str) -> dict:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    "https://gamma-api.polymarket.com/markets",
                    params={"conditionId": market_id, "limit": 1},
                    timeout=aiohttp.ClientTimeout(total=8),
                ) as resp:
                    if resp.status != 200:
                        return {}
                    rows = await resp.json()
                    if not isinstance(rows, list) or not rows:
                        return {}
                    return rows[0] or {}
        except Exception as exc:
            logger.debug(f"Gamma market lookup failed for {market_id}: {exc}")
            return {}

    async def get_active_leaders(self, conn: Any) -> list[Leader]:
        rows = await conn.fetch(
            """
            SELECT * FROM leaders
            WHERE excluded = FALSE AND on_watchlist = TRUE
            ORDER BY falcon_score DESC NULLS LAST
            """
        )
        return [Leader.from_row(r) for r in rows]

    async def get_leader_markets(self, conn: Any) -> set[str]:
        rows = await conn.fetch(
            """
            SELECT DISTINCT p.market_id
            FROM positions_reconstructed p
            JOIN leaders l ON p.wallet_address = l.wallet_address
            WHERE p.close_time IS NULL
              AND l.excluded = FALSE
              AND l.on_watchlist = TRUE
            """
        )
        return {r["market_id"] for r in rows}

    async def run(self) -> None:
        logger.info("LeaderRegistry started")
        while not self._stop.is_set():
            from src.database.connection import get_db

            try:
                async with get_db() as conn:
                    await self.refresh_leaderboard(conn)
                    await self.enrich_leaders(conn)
                    await self.sync_markets(conn)  # FIX 1
            except Exception as exc:
                logger.exception(f"LeaderRegistry cycle error: {exc}")

            # Wait for next refresh or stop
            stop_task = asyncio.ensure_future(self._stop.wait())
            try:
                await asyncio.wait([stop_task], timeout=settings.FALCON_REFRESH_INTERVAL_S)
                if stop_task.done():
                    break
            finally:
                if not stop_task.done():
                    stop_task.cancel()

    async def stop(self) -> None:
        self._stop.set()
        logger.info("LeaderRegistry stopping")
