"""
LeaderRegistry: maintains the leaders table and refreshes it on a schedule.

Pulls top-N wallets from Falcon agent 584, enriches each with agent 581 (Wallet 360),
classifies by strategy/influence/horizon/copiability, and persists to PostgreSQL.

Phase 3 Round 1 (Agent A) adds an event-driven refresh path on top of the
existing wall-clock loop. The ``run()`` cycle stays — 30 min FLOOR, never
relaxed. Between cycles, ``refresh_wallet(wallet, reason=...)`` performs a
targeted Falcon call (wallet360 + classification) for a single wallet,
gated by:

* An in-memory cooldown (``settings.EVENT_REFRESH_COOLDOWN_S``) so the
  same wallet isn't re-enriched more than every ~5 min.
* An in-memory ``asyncio.Event`` per wallet so duplicate concurrent
  calls coalesce into a single Falcon round-trip.
* A daily Falcon budget counter in Redis
  (``falcon:budget:YYYYMMDD`` TTL 25 h, default 500/day) so a flood of
  unknown wallets can't blow the whole day's quota.

External callers (Telegram /refresh, Agent D watchdog) invoke
``refresh_wallet`` directly with their own ``reason`` label; the event
bridge in ``src/registry/event_bridge.py`` does the same on the WS path.
"""

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import aiohttp
from loguru import logger

from src.config import settings
from src.database.connection import get_db  # Phase 3 Round 1: hoist for test-patchability
from src.observer.trade_observer import _market_type_label
from src.registry.falcon_client import FalconAPIError, FalconClient
from src.registry.models import Leader, LeaderClassification

# Phase 3 Round 1 (Agent A): metrics for event-driven refreshes. No-op
# fallback for early CI before the metrics module lands (same pattern as
# Phase 1 Task O / F).
try:
    from src.monitoring.metrics import (  # type: ignore[attr-defined]
        event_driven_refreshes_total,
        falcon_daily_budget_remaining,
    )
except Exception:  # pragma: no cover
    class _NoOpLabel:
        def labels(self, *_a, **_kw):
            return self

        def inc(self, *_a, **_kw):
            return None

        def set(self, *_a, **_kw):
            return None

    event_driven_refreshes_total = _NoOpLabel()  # type: ignore[assignment]
    falcon_daily_budget_remaining = _NoOpLabel()  # type: ignore[assignment]


def _falcon_budget_key(now: datetime | None = None) -> str:
    """Daily budget key in Redis. Day boundary is UTC midnight."""
    when = now or datetime.now(tz=timezone.utc)
    return f"falcon:budget:{when.strftime('%Y%m%d')}"


class LeaderRegistry:
    def __init__(self, falcon_client: FalconClient, redis_client: Any = None):
        self.falcon = falcon_client
        self.redis = redis_client
        self._stop = asyncio.Event()
        # Phase 3 Round 1: targeted-refresh coalescing + cooldown state.
        # Per-wallet asyncio.Event acts as a "refresh in flight" lock;
        # duplicate concurrent callers await the same event and observe
        # the result, so we never fan out 5× Falcon calls for the same
        # wallet from 5× trade messages arriving in burst.
        self._inflight: dict[str, asyncio.Event] = {}
        self._inflight_result: dict[str, bool] = {}
        # Wallet -> last successful refresh monotonic timestamp.
        self._last_refresh_at: dict[str, float] = {}

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
        # LIMIT 300 (was settings.INITIAL_LEADER_COUNT=200): with stale_refresh
        # piling up to 158+ from FK-upserted wallets, 200/cycle was barely
        # keeping pace. 300/cycle drains the queue in ~1 cycle even after
        # injects from new leaders the profiler has never seen.
        rows = await conn.fetch(
            """
            SELECT wallet_address FROM leaders
            WHERE (last_refresh IS NULL OR last_refresh < $1)
              AND excluded = FALSE
            LIMIT 300
            """,
            stale_cutoff,
        )
        count = 0
        skipped = 0
        for row in rows:
            wallet = row["wallet_address"]
            try:
                metrics = await self.falcon.get_wallet360(wallet)
            except FalconAPIError as e:
                logger.debug(f"Wallet360 unavailable for {wallet}: {e}")
                metrics = None
            if metrics is None:
                # Falcon doesn't know this wallet (typical for fresh wallets
                # injected via the profiler's FK upsert). Mark the wallet as
                # excluded + off-watchlist so it stops bloating the active
                # leader pool and the DQ "stale_refresh" counter. Historical
                # rows are preserved (we only flip flags). If Falcon ever
                # learns the wallet later, we can manually re-include it.
                await conn.execute(
                    """
                    UPDATE leaders
                    SET last_refresh = NOW(),
                        excluded = TRUE,
                        on_watchlist = FALSE,
                        exclude_reason = COALESCE(exclude_reason, 'falcon_no_data')
                    WHERE wallet_address = $1
                    """,
                    wallet,
                )
                skipped += 1
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
        logger.info(f"Enriched {count} leaders ({skipped} stamped no_data)")
        return count

    # ------------------------------------------------------------------ #
    # Phase 3 Round 1 — Event-driven refresh                              #
    # ------------------------------------------------------------------ #

    async def refresh_wallet(self, wallet: str, reason: str = "unknown") -> bool:
        """Targeted Falcon refresh of a single wallet.

        Designed for incremental, low-latency updates triggered by:
        * The event bridge on the WS path (``reason="ws_unknown_wallet"``)
        * The watchdog when freshness lags (``reason="watchdog"``)
        * External callers — Telegram /refresh, ops console
          (``reason="user_command"``)

        Returns True if the refresh succeeded (wallet row updated),
        False if the refresh was skipped (cooldown, budget exhausted,
        Falcon error). Idempotent: duplicate concurrent calls for the
        same wallet coalesce into a single Falcon round-trip via an
        in-memory ``asyncio.Event``.

        This method NEVER raises on Falcon failure — those are logged
        and surfaced via the ``event_driven_refreshes_total{result=...}``
        counter. The caller treats the return value as advisory.
        """
        wallet = (wallet or "").strip()
        if not wallet:
            return False

        # 1. Cooldown gate — cheap, in-memory. Avoids hammering Falcon
        # for the same wallet on every trade burst.
        cooldown_s = max(0, int(settings.EVENT_REFRESH_COOLDOWN_S))
        now_mono = time.monotonic()
        last_at = self._last_refresh_at.get(wallet, 0.0)
        if cooldown_s > 0 and (now_mono - last_at) < cooldown_s:
            event_driven_refreshes_total.labels(
                reason=reason, result="skipped_recent"
            ).inc()
            return False

        # 2. Coalesce concurrent callers. The first caller creates an
        # asyncio.Event; subsequent callers await it and observe the
        # cached result. Pattern documented in the class docstring.
        existing = self._inflight.get(wallet)
        if existing is not None:
            event_driven_refreshes_total.labels(
                reason=reason, result="coalesced"
            ).inc()
            await existing.wait()
            return self._inflight_result.get(wallet, False)

        event = asyncio.Event()
        self._inflight[wallet] = event
        self._inflight_result.pop(wallet, None)

        result = False
        try:
            # 3. Daily Falcon budget gate. Decrement BEFORE the API call
            # so a flood of unknown wallets can't blow the day's quota
            # before the first call returns.
            allowed = await self._reserve_falcon_budget()
            if not allowed:
                event_driven_refreshes_total.labels(
                    reason=reason, result="budget_exhausted"
                ).inc()
                return False

            # 4. Targeted Falcon round-trip + DB upsert. We hand the DB
            # work to ``_apply_wallet_refresh`` so the same body can be
            # called from a future Agent D "batch refresh" path without
            # duplicating the classification logic.
            try:
                metrics = await self.falcon.get_wallet360(wallet)
            except FalconAPIError as exc:
                logger.debug(
                    f"refresh_wallet({wallet}, reason={reason}): "
                    f"Falcon get_wallet360 failed: {exc}"
                )
                event_driven_refreshes_total.labels(
                    reason=reason, result="error"
                ).inc()
                return False

            try:
                await self._apply_wallet_refresh(wallet, metrics)
            except Exception as exc:
                logger.warning(
                    f"refresh_wallet({wallet}, reason={reason}): "
                    f"DB upsert failed: {exc}"
                )
                event_driven_refreshes_total.labels(
                    reason=reason, result="error"
                ).inc()
                return False

            self._last_refresh_at[wallet] = time.monotonic()
            event_driven_refreshes_total.labels(
                reason=reason, result="refreshed"
            ).inc()
            result = True
            return True
        finally:
            self._inflight_result[wallet] = result
            event.set()
            # Pop the event so the next caller after cooldown gets a
            # fresh Event (not the now-fired one).
            self._inflight.pop(wallet, None)

    async def refresh_now(self, reason: str = "user_command") -> int:
        """Force a full leaderboard + enrichment + sync_markets cycle now.

        Used by Telegram /refresh and any operator-initiated refresh
        path. Does NOT bypass Falcon rate limits — the underlying
        client's 60 RPM bucket still applies. Returns the number of
        leaders enriched in this cycle (0 on failure).
        """
        # get_db imported at module top (Phase 3 test-patchability)

        n = 0
        try:
            async with get_db() as conn:
                await self.refresh_leaderboard(conn)
                n = await self.enrich_leaders(conn)
                await self.sync_markets(conn)
                event_driven_refreshes_total.labels(
                    reason=reason, result="refreshed"
                ).inc()
        except Exception as exc:
            logger.exception(f"refresh_now({reason}) failed: {exc}")
            event_driven_refreshes_total.labels(
                reason=reason, result="error"
            ).inc()
        return n

    async def _apply_wallet_refresh(self, wallet: str, metrics: Any) -> None:
        """Apply a Falcon wallet360 result to the `leaders` row.

        Mirrors the per-wallet logic in ``enrich_leaders`` but for a
        single wallet (no LIMIT 300 batch). The leaders row is created
        if it doesn't already exist — event-driven refreshes can fire
        for wallets that aren't yet in the leaderboard.
        """
        # get_db imported at module top (Phase 3 test-patchability)

        async with get_db() as conn:
            if metrics is None:
                # falcon_no_data — same flag set as in `enrich_leaders`,
                # but we don't promote the wallet into the active pool.
                await conn.execute(
                    """
                    INSERT INTO leaders
                        (wallet_address, falcon_score, last_refresh,
                         excluded, on_watchlist, exclude_reason)
                    VALUES ($1, NULL, NOW(), TRUE, FALSE, 'falcon_no_data')
                    ON CONFLICT (wallet_address) DO UPDATE SET
                        last_refresh = NOW(),
                        excluded = TRUE,
                        on_watchlist = FALSE,
                        exclude_reason = COALESCE(
                            leaders.exclude_reason, 'falcon_no_data'
                        )
                    """,
                    wallet,
                )
                return
            w360 = metrics.to_dict()
            classification = self.classify_leader(w360)
            await conn.execute(
                """
                INSERT INTO leaders
                    (wallet_address, falcon_score, wallet360_json,
                     classification_json, excluded, exclude_reason,
                     last_refresh, on_watchlist)
                VALUES ($1, NULL, $2, $3, $4, $5, NOW(), TRUE)
                ON CONFLICT (wallet_address) DO UPDATE SET
                    wallet360_json = EXCLUDED.wallet360_json,
                    classification_json = EXCLUDED.classification_json,
                    excluded = EXCLUDED.excluded,
                    exclude_reason = EXCLUDED.exclude_reason,
                    last_refresh = NOW()
                """,
                wallet,
                json.dumps(w360),
                json.dumps(classification.model_dump()),
                not classification.copiable,
                "structural_bot" if classification.strategy == "structural" else None,
            )

    async def _reserve_falcon_budget(self) -> bool:
        """Decrement the daily Falcon budget. Returns True if a slot was
        reserved, False if exhausted.

        Backed by Redis ``falcon:budget:YYYYMMDD`` with TTL 25h. If
        ``self.redis`` is None (tests / cold boot), the budget is
        unbounded — the only ceiling is the Falcon RPM limiter inside
        the client.
        """
        budget_max = max(0, int(settings.FALCON_DAILY_BUDGET))
        if budget_max <= 0:
            # 0 means "disabled" — every event-driven refresh is allowed
            # and the gauge stays at 0.
            falcon_daily_budget_remaining.set(0)
            return True
        if self.redis is None:
            # No Redis attached — refresh path is unbudgeted (tests).
            falcon_daily_budget_remaining.set(float(budget_max))
            return True

        key = _falcon_budget_key()
        try:
            # INCR returns the new value. On first hit of the day the
            # key is missing, INCR creates it at 1 — we then attach a
            # 25h TTL so a forgotten EXPIRE doesn't leak the counter
            # across midnight.
            used = await self.redis.incr(key)
            # Set TTL only once per day (when used==1). EXPIRE is
            # idempotent but cheap.
            if used == 1:
                try:
                    await self.redis.expire(key, 25 * 3600)
                except Exception:
                    pass
            remaining = max(0, budget_max - int(used))
            falcon_daily_budget_remaining.set(float(remaining))
            if int(used) > budget_max:
                # We crossed the line on this call — back out the
                # increment so subsequent calls see the same `used`.
                try:
                    await self.redis.decr(key)
                except Exception:
                    pass
                return False
            return True
        except Exception as exc:
            # Redis failure — fail-open so the user-facing path isn't
            # blocked by a transient outage. The hard rate cap inside
            # FalconClient (60 RPM) still applies.
            logger.debug(f"_reserve_falcon_budget: Redis incr failed: {exc}")
            return True

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
        """Upsert recent market metadata that is missing or stale.

        Filters out markets whose end_date has already passed by more than
        24 hours: they are resolved/dead and will never produce another
        trade signal worth modelling. This keeps sync_markets focused on
        the live pool and prevents the "unmapped_tokens" DQ counter from
        being inflated by old expired markets the bot will never trade.
        """
        rows = await conn.fetch(
            """
            SELECT DISTINCT t.market_id FROM trades_observed t
            LEFT JOIN markets m ON m.market_id = t.market_id
            WHERE t.time > NOW() - INTERVAL '7 days'
              AND (m.end_date IS NULL OR m.end_date > NOW() - INTERVAL '24 hours')
              AND t.market_id NOT IN (
                  SELECT market_id FROM markets
                  WHERE updated_at > NOW() - INTERVAL '24 hours'
                    AND volume_24h IS NOT NULL
                    AND token_yes IS NOT NULL
                    AND token_no IS NOT NULL
              )
            LIMIT 300
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
            fee_rate = float(m.get("makerBaseFee") or 0)

            # Phase 0 Task C fix (audit MG-3): `markets.liquidity_score`
            # must come from Falcon agent 575 (Market Insights) — the
            # documented source per master CLAUDE.md §6,
            # `src/profiler/CLAUDE.md:172` and `error_model.py:83/220`.
            # The previous implementation wrote agent 574's raw
            # `liquidity` field, which is a USD depth, not a normalized
            # 0–1 score, and silently desynced from the documented
            # methodology. We now try 575 first and only fall through
            # to 574's `liquidity` (and Gamma's `liquidity`) so the
            # legacy callers don't lose their value when 575 is
            # transiently unavailable. The `liquidity_score_source`
            # column tags each row with provenance so a backfill audit
            # can distinguish 575 / 574 / gamma rows.
            liquidity_score: float | None = None
            liquidity_source: str | None = None
            try:
                insights = await self.falcon.get_market_insights(mid)
            except Exception as exc:  # defensive — get_market_insights swallows FalconAPIError
                logger.debug(f"Market Insights call raised for {mid}: {exc}")
                insights = None
            if insights is not None:
                liquidity_score = float(insights.liquidity_score)
                liquidity_source = "falcon_575"
            elif m.get("liquidity") is not None:
                # Fallback A: agent 574's raw `liquidity` field, or the
                # Gamma response (which uses the same field name). This
                # is the pre-Task-C behaviour — kept as a transitional
                # safety net so the column doesn't go NULL on every row
                # the day agent 575 is rate-limited.
                liquidity_score = float(m.get("liquidity") or 0)
                liquidity_source = (
                    "gamma" if m.get("clobTokenIds") or m.get("endDateIso") else "falcon_574"
                )

            try:
                await conn.execute(
                    """
                    INSERT INTO markets
                        (market_id, question, category, token_yes, token_no,
                         end_date, volume_24h, liquidity_score, fee_rate_pct,
                         liquidity_score_updated_at, liquidity_score_source,
                         updated_at)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,
                            CASE WHEN $8::numeric IS NULL THEN NULL ELSE NOW() END,
                            $10,
                            NOW())
                    ON CONFLICT (market_id) DO UPDATE SET
                        question       = EXCLUDED.question,
                        category       = EXCLUDED.category,
                        token_yes      = COALESCE(EXCLUDED.token_yes, markets.token_yes),
                        token_no       = COALESCE(EXCLUDED.token_no, markets.token_no),
                        end_date       = COALESCE(EXCLUDED.end_date, markets.end_date),
                        volume_24h     = EXCLUDED.volume_24h,
                        liquidity_score= COALESCE(
                            EXCLUDED.liquidity_score,
                            markets.liquidity_score
                        ),
                        liquidity_score_updated_at = CASE
                            WHEN EXCLUDED.liquidity_score IS NOT NULL THEN NOW()
                            ELSE markets.liquidity_score_updated_at
                        END,
                        liquidity_score_source = COALESCE(
                            EXCLUDED.liquidity_score_source,
                            markets.liquidity_score_source
                        ),
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
                    liquidity_score,
                    fee_rate,
                    liquidity_source,
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

    async def recategorize_unknowns(self, conn: Any, max_markets: int = 1000) -> dict:
        """Re-run text inference on markets stuck at category='unknown'.

        These are typically markets whose first trade arrived via WebSocket
        without a question hint, so `_repair_market_from_trade_hint` short-
        circuited and the stub stayed at 'unknown'. Once Falcon enrichment
        or a later REST poll populates the question, the inference can
        succeed — but only if something re-runs it. This method is that
        retry, called once per registry cycle.

        Also propagates the new category to historical trades_observed and
        positions_reconstructed rows so wallet-centric aggregations see the
        upgraded value immediately.
        """
        rows = await conn.fetch(
            """
            SELECT market_id, question, category
            FROM markets
            WHERE COALESCE(NULLIF(category, ''), 'unknown') IN ('unknown', 'none', 'null')
              AND question IS NOT NULL
              AND question NOT LIKE 'Market 0x%'
            LIMIT $1
            """,
            max_markets,
        )
        upgraded = 0
        unchanged = 0
        for row in rows:
            mid = row["market_id"]
            new_cat = _market_type_label(row["category"], row["question"])
            if new_cat == "unknown" or new_cat == (row["category"] or ""):
                unchanged += 1
                continue
            try:
                await conn.execute(
                    "UPDATE markets SET category = $2, updated_at = NOW() WHERE market_id = $1",
                    mid, new_cat,
                )
                # Backfill the denormalized columns on historical rows.
                await conn.execute(
                    """
                    UPDATE trades_observed
                    SET category = $2
                    WHERE market_id = $1
                      AND COALESCE(NULLIF(category, ''), 'unknown') IN ('unknown', 'none', 'null')
                    """,
                    mid, new_cat,
                )
                await conn.execute(
                    """
                    UPDATE positions_reconstructed
                    SET category = $2
                    WHERE market_id = $1
                      AND COALESCE(NULLIF(category, ''), 'unknown') IN ('unknown', 'none', 'null')
                    """,
                    mid, new_cat,
                )
                upgraded += 1
            except Exception as exc:
                logger.debug(f"recategorize_unknowns failed for {mid}: {exc}")
        if upgraded or unchanged:
            logger.info(f"recategorize_unknowns: {upgraded} upgraded, {unchanged} still unknown")
        return {"upgraded": upgraded, "unchanged": unchanged, "scanned": len(rows)}

    async def run(self) -> None:
        logger.info("LeaderRegistry started")
        while not self._stop.is_set():
            from src.database.connection import get_db

            try:
                async with get_db() as conn:
                    await self.refresh_leaderboard(conn)
                    await self.enrich_leaders(conn)
                    await self.sync_markets(conn)  # FIX 1
                    await self.recategorize_unknowns(conn)
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
