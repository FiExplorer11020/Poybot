"""
Behavior Profiler — per-leader behavioral profile built incrementally from closed positions.
Subscribes to Redis positions:closed. Updates profile_json in leader_profiles table.
"""

import asyncio
import copy
import json
import math
from datetime import datetime, timezone

from loguru import logger

from src.config import settings
from src.control.redis_pubsub import Subscriber
from src.control.redis_streams import StreamConsumer
from src.database.connection import get_db
from src.economics.models import ECONOMIC_MODEL_VERSION
from src.economics.versioning import valid_paper_trade_filter

REDIS_POSITIONS_CHANNEL = "positions:closed"
# Phase 2 / legacy fan-out — kept as a TODO(phase3-round2) safety net
# while the new Streams path soaks; remove once we've gone several
# weeks without seeing a stream-vs-pubsub divergence in production.
REDIS_TRADES_CHANNEL = "trades:observed"
# Phase 3 round 1: the durable, consumer-group-backed equivalent.
TRADES_STREAM_NAME = "trades:stream"
TRADES_STREAM_GROUP = "profiler.behavior"

# Idempotency cache size. ~5k entries covers ~5 minutes of peak
# trade volume (1k/min). Tuned for "double-write window" only —
# not "process lifetime".
_SEEN_TRADE_KEYS_MAXSIZE = 5_000


def _trade_dedup_key(event: dict) -> str:
    """Canonical fingerprint of a trade event for idempotent dispatch.

    Mirrors the fields trade_observer uses for its DB-level UNIQUE
    INDEX so the keys collide for `(pub/sub, stream)` twin-publishes
    and for any XCLAIM replay.
    """
    wallet = event.get("wallet_address") or ""
    market = event.get("market_id") or ""
    t = event.get("time") or ""
    side = event.get("side") or ""
    price = event.get("price") or ""
    size = event.get("size_usdc") or ""
    if not wallet or not market:
        return ""
    return f"{wallet}|{market}|{t}|{side}|{price}|{size}"
RECENT_LOSS_LIMIT = 25
PROCESS_SCALE_WINDOW_S = 1800
PROCESS_BURST_WINDOW_S = 180
V1_PAPER_TRADE_PT_SQL = valid_paper_trade_filter("pt")
V1_PROFILE_TABLE_SQL = (
    "leader_profiles.economic_model_version = 'v1.0.0' "
    "AND leader_profiles.learning_invalidated_at IS NULL"
)


def _default_decision_bucket() -> dict:
    return {
        "wins": 0,
        "losses": 0,
        "beta_a": 1.0,
        "beta_b": 1.0,
        "avg_win_pnl": 0.0,
        "avg_loss_pnl": 0.0,
        "avg_win_confidence": 0.0,
        "avg_loss_confidence": 0.0,
        "reason_stats": {},
    }


def _default_process_state() -> dict:
    return {
        "orders_seen": 0,
        "transitions_seen": 0,
        "buy_count": 0,
        "sell_count": 0,
        "avg_order_size": 0.0,
        "ewma_order_size": 0.0,
        "avg_interarrival_s": 0.0,
        "flip_rate": 0.0,
        "scale_in_rate": 0.0,
        "process_score_ewma": 0.5,
        "category_counts": {},
        "category_last_seen_at": {},
        "last_order": {},
    }


def _default_profile() -> dict:
    return {
        "preferred_categories": {},
        "sizing": {"ewma_size": 0.0, "avg_size": 0.0},
        "entry_patterns": {"contrarian_rate": 0.0, "momentum_rate": 0.0, "trades_count": 0},
        "accuracy": {"overall": 0.0, "resolved_count": 0, "by_category": {}},
        "follower_impact": {"avg_volume_induced": 0.0, "avg_price_move": 0.0},
        "decision_process": _default_process_state(),
        "decision_learning": {
            "follow": _default_decision_bucket(),
            "fade": _default_decision_bucket(),
        },
        "loss_analysis": {"recent_losses": [], "last_position_loss_at": None},
    }


class BehaviorProfiler:
    def __init__(self, redis_client, error_model=None):
        self._redis = redis_client
        self._error_model = error_model
        self._running = False
        self._stop_event = asyncio.Event()
        # F-04 + Phase 3 round 1: trades flow via a Streams consumer
        # group (durable, at-least-once); positions:closed remains on
        # pub/sub for now because Phase 3 round 1 only re-plumbs the
        # trades:observed path. The pubsub Subscriber here keeps both
        # the positions handler AND a TODO(phase3-round2) dual-read of
        # trades:observed as a redundancy net during the soak.
        self._subscriber: Subscriber | None = None
        self._trades_consumer: StreamConsumer | None = None
        # Idempotency guard: dual-write (pub/sub + stream) and any
        # Streams replay (XCLAIM after a crash mid-handler) both
        # produce identical `event` dicts. The set holds canonical
        # `(wallet, market, time, side, price, size)` keys so the
        # second arrival is dropped before any Dirichlet/Beta update
        # runs. Size is bounded by `_SEEN_TRADE_KEYS_MAXSIZE`.
        self._seen_trade_keys: set[str] = set()

    async def start(self) -> None:
        self._running = True
        self._stop_event.clear()
        if self._redis is None:
            return
        # Subscriber still owns positions:closed (pub/sub, not migrated).
        # It ALSO keeps trades:observed as a TODO(phase3-round2) safety
        # net — when the soak proves the Streams path is stable we'll
        # drop the dual-subscribe.
        self._subscriber = Subscriber(
            settings.REDIS_URL, name="profiler.behavior"
        )
        self._subscriber.register(
            REDIS_POSITIONS_CHANNEL, self._on_position_message
        )
        # TODO(phase3-round2): remove this pubsub subscription once the
        # Streams path has soaked. Trade handlers are guarded by
        # `_seen_trade_keys` (process-local) so the duplicate read does
        # NOT double-update the Dirichlet/Beta posteriors.
        self._subscriber.register(REDIS_TRADES_CHANNEL, self._on_trade_message)

        # Phase 3 round 1: durable trades consumer group. IDEMPOTENT
        # contract: re-processing the same `(market_id, wallet, time,
        # side, price, size_usdc)` tuple is suppressed by
        # `_seen_trade_keys` below, so a Streams replay (XCLAIM after a
        # crash mid-handler) doesn't double-count.
        self._trades_consumer = StreamConsumer(
            settings.REDIS_URL,
            stream=TRADES_STREAM_NAME,
            group=TRADES_STREAM_GROUP,
            consumer_name=f"{TRADES_STREAM_GROUP}.1",
        )
        self._trades_consumer.register(self._on_trade_stream_entry)
        await self._subscriber.start()
        await self._trades_consumer.start()
        try:
            await self._stop_event.wait()
        except asyncio.CancelledError:
            pass

    async def stop(self) -> None:
        self._running = False
        self._stop_event.set()
        if self._trades_consumer is not None:
            await self._trades_consumer.stop()
            self._trades_consumer = None
        if self._subscriber is not None:
            await self._subscriber.stop()
            self._subscriber = None

    async def _on_position_message(self, event: dict, _channel: str) -> None:
        if not self._running:
            return
        try:
            await self.on_position_closed(event)
        except Exception as e:
            logger.error(f"BehaviorProfiler error: {e}")
            raise

    async def _on_trade_message(self, event: dict, _channel: str) -> None:
        """Pub/sub fallback handler. Delegates to the same idempotent path."""
        if not self._running:
            return
        await self._dispatch_trade_event(event, source="pubsub", entry_id=None)

    async def _on_trade_stream_entry(
        self, event: dict, _stream: str, entry_id: str
    ) -> None:
        """Streams handler — receives `(payload, stream, entry_id)`.

        IDEMPOTENT: the `_seen_trade_keys` LRU below ignores a repeat
        publish of the same `(wallet, market, time, side, price, size)`
        tuple. Trade-observer's dual-write (pub/sub + streams) and a
        Streams XCLAIM replay both go through here, so the handler is
        exercised on every code path.
        """
        if not self._running:
            return
        await self._dispatch_trade_event(event, source="stream", entry_id=entry_id)

    async def _dispatch_trade_event(
        self, event: dict, *, source: str, entry_id: str | None
    ) -> None:
        # Idempotency guard. The keys are short — wallet:market:time-ms
        # is enough because trade_observer enforces a `(wallet, market,
        # bucket, side, price, size)` dedup at ingest. We just need to
        # suppress the (pub/sub, stream) twin-publish AND any XCLAIM
        # replay; both produce IDENTICAL `event` dicts so a hash of
        # the canonical fields collides.
        key = _trade_dedup_key(event)
        if key and key in self._seen_trade_keys:
            return
        if key:
            self._seen_trade_keys.add(key)
            # Bound the memory footprint — drop the oldest half once
            # we hit a soft cap. The pub/sub + streams dual-write +
            # ~1k entries/min peak gives plenty of room.
            if len(self._seen_trade_keys) > _SEEN_TRADE_KEYS_MAXSIZE:
                # popleft-ish behaviour on a set: drop ~half by
                # rebuilding from the most recent inserts. Fine for
                # the cardinality we're talking about.
                self._seen_trade_keys = set(
                    list(self._seen_trade_keys)[
                        -(_SEEN_TRADE_KEYS_MAXSIZE // 2):
                    ]
                )
        try:
            await self.on_leader_trade(event)
        except Exception as e:
            # IMPORTANT: we re-raise from the stream handler so the
            # StreamConsumer's retry path (XCLAIM after claim_idle_ms)
            # gets a shot. The pub/sub Subscriber catches exceptions
            # anyway, so this branch is safe for both sources.
            logger.error(
                f"BehaviorProfiler trade error src={source} "
                f"entry_id={entry_id}: {e}"
            )
            raise

    async def on_position_closed(self, event: dict) -> None:
        """Update behavioral profile for the leader of this closed position."""
        wallet = event.get("wallet_address", "")
        if not wallet:
            return

        pnl_usdc = float(event.get("pnl_usdc") or 0)
        category = event.get("category", "unknown")
        size_usdc = float(event.get("size_usdc") or 0)
        is_contrarian = bool(event.get("is_contrarian", False))

        # Load current profile
        (
            profile,
            positions_resolved,
            trades_observed_count,
            profile_maturity,
        ) = await self._load_profile(wallet)
        confirmed_followers = await self._count_confirmed_followers(wallet)
        trade_context = await self._build_error_trade_context(
            profile,
            event,
            profile_maturity=profile_maturity,
            confirmed_followers=confirmed_followers,
        )

        # 2. Update EWMA sizing FIRST so the size-weighted updates below
        #    have a stable baseline to normalise against.
        if size_usdc > 0:
            _update_sizing(profile, size_usdc)

        # 1. Update Dirichlet category preferences (size-weighted)
        _update_dirichlet(profile, category, size_usdc=size_usdc)

        # 3. Update entry patterns
        _update_entry_patterns(profile, is_contrarian)

        # 4. Update accuracy (size-weighted Beta posterior)
        win = pnl_usdc > 0
        _update_accuracy(profile, category, win, size_usdc=size_usdc)

        new_resolved = positions_resolved + 1

        # 5. Compute maturity
        maturity = _compute_maturity(new_resolved, confirmed_followers)
        if not win:
            profile.setdefault("loss_analysis", {}).setdefault("recent_losses", [])
            profile["loss_analysis"]["last_position_loss_at"] = (
                event.get("close_time")
                or event.get("time")
                or datetime.now(tz=timezone.utc).isoformat()
            )

        # 6. Persist
        await self._save_profile(wallet, profile, trades_observed_count, new_resolved, maturity)
        if self._error_model is not None:
            try:
                await self._error_model.update(
                    wallet,
                    {
                        "category": category,
                        "pnl_usdc": pnl_usdc,
                        "trade_context": trade_context,
                    },
                )
            except Exception as e:
                logger.warning(f"ErrorModel update failed for {wallet}: {e}")

    async def on_leader_trade(self, trade: dict) -> None:
        """Continuously learn a wallet's order-flow process from every leader trade."""
        if not trade.get("is_leader"):
            return

        wallet = trade.get("wallet_address", "")
        market_id = trade.get("market_id", "")
        side = (trade.get("side") or "").upper()
        if not wallet or not market_id or side not in {"BUY", "SELL"}:
            return

        try:
            size_usdc = float(trade.get("size_usdc") or 0.0)
            ts = trade.get("time")
            if isinstance(ts, str):
                trade_time = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            else:
                trade_time = datetime.now(tz=timezone.utc)
        except (TypeError, ValueError):
            return

        category = (
            trade.get("market_category")
            or trade.get("category")
            or await self._get_market_category(market_id)
        )
        profile, positions_resolved, trades_observed_count, maturity = await self._load_profile(
            wallet
        )
        _update_decision_process(
            profile,
            {
                "market_id": market_id,
                "side": side,
                "size_usdc": size_usdc,
                "category": category,
                "time": trade_time.isoformat(),
            },
        )
        await self._save_profile(
            wallet,
            profile,
            trades_observed_count + 1,
            positions_resolved,
            maturity,
        )

    async def record_decision_outcome(self, wallet: str, outcome: dict) -> dict:
        """
        Persist FOLLOW/FADE outcome learning in leader_profiles.

        This is the canonical persisted memory for what the strategy learned from
        each paper-trade win/loss.
        """
        if not wallet:
            return {"reason_codes": [], "penalty": 0.0}

        (
            profile,
            positions_resolved,
            trades_observed_count,
            profile_maturity,
        ) = await self._load_profile(wallet)

        action = outcome.get("action", "")
        if action not in {"follow", "fade"}:
            return {"reason_codes": [], "penalty": 0.0}

        won = bool(outcome.get("won", False))
        pnl_usdc = float(outcome.get("pnl_usdc") or 0.0)
        confidence = float(outcome.get("confidence") or 0.0)
        trade_context = dict(outcome.get("trade_context") or {})
        trade_context.setdefault("profile_maturity", profile_maturity)
        if not _is_valid_learning_sample(
            trade_context=trade_context,
            close_reason=outcome.get("close_reason"),
            opened_at=outcome.get("opened_at"),
            closed_at=outcome.get("closed_at"),
        ):
            return {"reason_codes": [], "penalty": 0.0, "ignored": True}

        reason_codes = _infer_reason_codes(
            profile=profile,
            action=action,
            trade_context=trade_context,
            confidence=confidence,
            close_reason=outcome.get("close_reason"),
        )
        _update_decision_learning(
            profile=profile,
            action=action,
            won=won,
            pnl_usdc=pnl_usdc,
            confidence=confidence,
            reason_codes=reason_codes,
            market_id=outcome.get("market_id", ""),
            close_reason=outcome.get("close_reason", ""),
            event_time=outcome.get("closed_at") or datetime.now(tz=timezone.utc).isoformat(),
            trade_context=trade_context,
        )

        await self._save_profile(
            wallet=wallet,
            profile=profile,
            trades_observed_count=trades_observed_count,
            positions_resolved=positions_resolved,
            maturity=profile_maturity,
        )
        penalty = _reason_penalty_from_profile(profile, action, reason_codes)
        return {"reason_codes": reason_codes, "penalty": penalty}

    async def rebuild_decision_learning(self, wallet: str | None = None) -> dict:
        """
        Rebuild persisted decision-learning state from historical closed paper trades.

        This is idempotent: it rewrites only decision-learning-related sections of
        profile_json and leaves the rest of the behavioral profile intact.
        """
        try:
            trade_rows = await self._fetch_closed_paper_trades(wallet)
        except Exception as exc:
            logger.error(f"Failed to fetch closed paper trades for rebuild: {exc}")
            return {"wallets": 0, "trades": 0}

        if not trade_rows:
            return {"wallets": 0, "trades": 0}

        by_wallet: dict[str, list[dict]] = {}
        for row in trade_rows:
            by_wallet.setdefault(row["leader_wallet"], []).append(row)

        rebuilt_wallets = 0
        rebuilt_trades = 0
        ignored_trades = 0

        for wallet_address, rows in by_wallet.items():
            profile, positions_resolved, trades_observed, maturity = await self._load_profile(
                wallet_address
            )
            _reset_decision_learning(profile)
            confirmed_followers = await self._count_confirmed_followers(wallet_address)

            for row in rows:
                context = self._compose_backfill_trade_context(
                    profile=profile,
                    row=row,
                    maturity=maturity,
                    confirmed_followers=confirmed_followers,
                )
                if not _is_valid_learning_sample(
                    trade_context=context,
                    close_reason=row.get("close_reason"),
                    opened_at=row.get("opened_at"),
                    closed_at=row.get("closed_at"),
                ):
                    ignored_trades += 1
                    continue
                action = row.get("strategy", "")
                confidence = float(row.get("confidence") or 0.0)
                reason_codes = _infer_reason_codes(
                    profile=profile,
                    action=action,
                    trade_context=context,
                    confidence=confidence,
                    close_reason=row.get("close_reason"),
                )
                _update_decision_learning(
                    profile=profile,
                    action=action,
                    won=float(row.get("pnl_usdc") or 0.0) > 0.0,
                    pnl_usdc=float(row.get("pnl_usdc") or 0.0),
                    confidence=confidence,
                    reason_codes=reason_codes,
                    market_id=row.get("market_id", ""),
                    close_reason=row.get("close_reason", ""),
                    event_time=(
                        row.get("closed_at").isoformat()
                        if row.get("closed_at") is not None
                        else datetime.now(tz=timezone.utc).isoformat()
                    ),
                    trade_context=context,
                )
                rebuilt_trades += 1

            await self._save_profile(
                wallet=wallet_address,
                profile=profile,
                trades_observed_count=trades_observed,
                positions_resolved=positions_resolved,
                maturity=maturity,
            )
            rebuilt_wallets += 1

        logger.info(
            f"Rebuilt decision learning for {rebuilt_wallets} wallets "
            f"from {rebuilt_trades} closed paper trades "
            f"(ignored {ignored_trades} invalid historical samples)"
        )
        return {"wallets": rebuilt_wallets, "trades": rebuilt_trades}

    async def get_profile(self, wallet: str) -> dict | None:
        """Return the current profile dict for a wallet, or None if not found."""
        try:
            async with get_db() as conn:
                row = await conn.fetchrow(
                    "SELECT profile_json FROM leader_profiles WHERE wallet_address = $1",
                    wallet,
                )
                if row:
                    data = row["profile_json"]
                    if isinstance(data, str):
                        profile = json.loads(data)
                    else:
                        profile = dict(data) if data else None
                    if profile is not None:
                        _ensure_profile_schema(profile)
                    return profile
        except Exception as e:
            logger.error(f"get_profile error: {e}")
        return None

    def get_deviation_score(self, profile: dict, trade: dict) -> float:
        """
        How much does this trade deviate from the leader's typical behavior?
        Returns 0.0 (normal) to 1.0 (very unusual).

        Factors:
        - Category: not in top preferred categories → high deviation
        - Size: outside EWMA ± 2 std → high deviation
        - Direction: contrarian when leader is normally momentum (or vice versa)
        """
        return _compute_deviation_score(profile, trade)

    def get_reason_codes(
        self,
        profile: dict,
        action: str,
        trade_context: dict,
        confidence: float = 0.0,
        close_reason: str | None = None,
    ) -> list[str]:
        return _infer_reason_codes(profile, action, trade_context, confidence, close_reason)

    def get_reason_penalty(self, profile: dict, action: str, trade_context: dict) -> float:
        reason_codes = self.get_reason_codes(profile, action, trade_context)
        return _reason_penalty_from_profile(profile, action, reason_codes)

    def get_process_insights(self, profile: dict, trade: dict) -> dict:
        return _compute_process_insights(profile, trade)

    def get_process_score(self, profile: dict, trade: dict) -> float:
        return float(self.get_process_insights(profile, trade).get("process_score", 0.5))

    async def _load_profile(self, wallet: str) -> tuple[dict, int, int, float]:
        """Load profile from DB. Returns profile dict plus counters and maturity."""
        try:
            async with get_db() as conn:
                row = await conn.fetchrow(
                    f"""
                    SELECT profile_json, positions_resolved, trades_observed, profile_maturity
                    FROM leader_profiles
                    WHERE wallet_address = $1
                      AND {V1_PROFILE_TABLE_SQL}
                    """,
                    wallet,
                )
                if row:
                    data = row["profile_json"]
                    profile = (
                        json.loads(data)
                        if isinstance(data, str)
                        else (dict(data) if data else _default_profile())
                    )
                    _ensure_profile_schema(profile)
                    return (
                        profile,
                        row["positions_resolved"] or 0,
                        row["trades_observed"] or 0,
                        float(row["profile_maturity"] if "profile_maturity" in row else 0.0),
                    )
        except Exception as e:
            logger.debug(f"Profile load error: {e}")
        return _default_profile(), 0, 0, 0.0

    async def _save_profile(
        self,
        wallet: str,
        profile: dict,
        trades_observed_count: int,
        positions_resolved: int,
        maturity: float,
    ) -> None:
        try:
            async with get_db() as conn:
                # Ensure the wallet exists in `leaders` before we touch
                # leader_profiles — the FK leader_profiles_wallet_address_fkey
                # would otherwise raise on observed wallets that the registry
                # hasn't ingested yet (race at boot, or a wallet picked up
                # via WebSocket before the hourly Falcon refresh). Inserting
                # with ON CONFLICT DO NOTHING is idempotent: the registry
                # will still enrich the row on its next pass.
                await conn.execute(
                    """
                    INSERT INTO leaders (wallet_address, on_watchlist, excluded, first_seen)
                    VALUES ($1, FALSE, FALSE, NOW())
                    ON CONFLICT (wallet_address) DO NOTHING
                    """,
                    wallet,
                )
                await conn.execute(
                    """
                    INSERT INTO leader_profiles
                        (wallet_address, profile_json, trades_observed, positions_resolved,
                         profile_maturity, economic_model_version, last_updated)
                    VALUES ($1, $2::jsonb, $3, $4, $5, $6, NOW())
                    ON CONFLICT (wallet_address) DO UPDATE SET
                        profile_json        = EXCLUDED.profile_json,
                        trades_observed     = EXCLUDED.trades_observed,
                        positions_resolved  = EXCLUDED.positions_resolved,
                        profile_maturity    = EXCLUDED.profile_maturity,
                        economic_model_version = EXCLUDED.economic_model_version,
                        last_updated        = EXCLUDED.last_updated
                    """,
                    wallet,
                    json.dumps(profile),
                    trades_observed_count,
                    positions_resolved,
                    round(maturity, 4),
                    ECONOMIC_MODEL_VERSION,
                )
        except Exception as e:
            logger.error(f"Profile save error: {e}")

    async def _count_confirmed_followers(self, wallet: str) -> int:
        try:
            async with get_db() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT COUNT(*) AS cnt FROM follower_edges
                    WHERE leader_wallet = $1
                      AND co_occurrences >= 5
                      AND same_direction_rate >= 0.7
                    """,
                    wallet,
                )
                return int(row["cnt"]) if row else 0
        except Exception as e:
            logger.debug(f"Follower count error: {e}")
        return 0

    async def _fetch_closed_paper_trades(self, wallet: str | None = None) -> list[dict]:
        where_sql = (
            "WHERE pt.status = 'closed' "
            "AND pt.strategy IN ('follow', 'fade') "
            f"AND {V1_PAPER_TRADE_PT_SQL}"
        )
        params: list = []
        if wallet:
            where_sql += " AND pt.leader_wallet = $1"
            params.append(wallet)

        query = f"""
            SELECT
                pt.leader_wallet,
                pt.market_id,
                pt.token_id,
                pt.strategy,
                pt.entry_price,
                pt.exit_price,
                pt.size_usdc,
                pt.pnl_usdc,
                pt.confidence,
                pt.close_reason,
                pt.opened_at,
                pt.closed_at,
                pt.leader_context,
                m.category,
                m.liquidity_score
            FROM paper_trades pt
            LEFT JOIN markets m ON m.market_id = pt.market_id
            {where_sql}
            ORDER BY pt.leader_wallet, pt.closed_at NULLS LAST, pt.id
        """
        async with get_db() as conn:
            rows = await conn.fetch(query, *params)
        normalized: list[dict] = []
        for row in rows:
            record = dict(row)
            raw_context = record.get("leader_context")
            if isinstance(raw_context, str):
                try:
                    record["leader_context"] = json.loads(raw_context)
                except json.JSONDecodeError:
                    record["leader_context"] = {}
            elif raw_context is None:
                record["leader_context"] = {}
            else:
                record["leader_context"] = dict(raw_context)
            normalized.append(record)
        return normalized

    async def _build_error_trade_context(
        self,
        profile: dict,
        event: dict,
        profile_maturity: float = 0.0,
        confirmed_followers: int = 0,
    ) -> dict:
        market_id = event.get("market_id", "")
        category = event.get("category", "unknown")
        liquidity_score = await self._get_market_liquidity(market_id)
        size_usdc = float(event.get("size_usdc") or 0.0)
        is_contrarian = bool(event.get("is_contrarian", False))
        event_time = (
            event.get("open_time")
            or event.get("time")
            or event.get("close_time")
            or datetime.now(tz=timezone.utc).isoformat()
        )
        trade = {
            "category": category,
            "size_usdc": size_usdc,
            "is_contrarian": is_contrarian,
            "market_id": market_id,
            "side": "BUY",
            "time": event_time,
        }
        deviation_score = _compute_deviation_score(profile, trade)
        process_insights = _compute_process_insights(profile, trade)
        ewma_size = float(profile.get("sizing", {}).get("ewma_size", 0.0) or 0.0)
        size_ratio = size_usdc / ewma_size if ewma_size > 0 and size_usdc > 0 else 1.0
        category_accuracy = _get_category_accuracy(profile, category)
        hours_since_last_loss = _hours_since_position_loss(profile, event_time)
        hours_since_category_trade = _hours_since_category_trade(profile, category, event_time)
        time_features = _cyclical_time_features(event_time)
        return {
            "category": category,
            "is_contrarian": is_contrarian,
            "deviation_score": round(deviation_score, 4),
            "size_ratio": round(size_ratio, 4),
            "liquidity_score": round(liquidity_score, 4),
            "process_score": process_insights["process_score"],
            "flip_rate": process_insights["flip_rate"],
            "scale_in_rate": process_insights["scale_in_rate"],
            "avg_interarrival_s": process_insights["avg_interarrival_s"],
            "interarrival_s": process_insights["interarrival_s"],
            "flip_flag": process_insights["flip_flag"],
            "scale_in_flag": process_insights["scale_in_flag"],
            "hours_since_last_trade": process_insights["hours_since_last_trade"],
            "hours_since_category_last_trade": hours_since_category_trade,
            "hours_since_last_loss": hours_since_last_loss,
            "category_accuracy": round(category_accuracy, 4),
            "profile_maturity": round(float(profile_maturity or 0.0), 4),
            "confirmed_followers": int(confirmed_followers or 0),
            **time_features,
        }

    async def _fetch_leader_trades(self, wallet: str | None = None) -> list[dict]:
        where_sql = "WHERE t.is_leader = TRUE"
        params: list = []
        if wallet:
            where_sql += " AND t.wallet_address = $1"
            params.append(wallet)

        query = f"""
            SELECT
                t.wallet_address AS leader_wallet,
                t.market_id,
                t.token_id,
                t.side,
                t.size_usdc,
                t.time,
                m.category
            FROM trades_observed t
            LEFT JOIN markets m ON m.market_id = t.market_id
            {where_sql}
            ORDER BY t.wallet_address, t.time, t.id
        """
        async with get_db() as conn:
            rows = await conn.fetch(query, *params)
        return [dict(r) for r in rows]

    async def rebuild_order_process(self, wallet: str | None = None) -> dict:
        """
        Rebuild decision_process state from historical observed leader orders.
        """
        try:
            trade_rows = await self._fetch_leader_trades(wallet)
        except Exception as exc:
            logger.error(f"Failed to fetch leader trades for process rebuild: {exc}")
            return {"wallets": 0, "orders": 0}

        if not trade_rows:
            return {"wallets": 0, "orders": 0}

        by_wallet: dict[str, list[dict]] = {}
        for row in trade_rows:
            by_wallet.setdefault(row["leader_wallet"], []).append(row)

        rebuilt_wallets = 0
        rebuilt_orders = 0
        for wallet_address, rows in by_wallet.items():
            profile, positions_resolved, trades_observed, maturity = await self._load_profile(
                wallet_address
            )
            _reset_order_process(profile)
            for row in rows:
                _update_decision_process(
                    profile,
                    {
                        "market_id": row.get("market_id", ""),
                        "side": row.get("side", ""),
                        "size_usdc": float(row.get("size_usdc") or 0.0),
                        "category": row.get("category") or "unknown",
                        "time": row["time"].isoformat() if row.get("time") is not None else None,
                    },
                )
                rebuilt_orders += 1
            await self._save_profile(
                wallet=wallet_address,
                profile=profile,
                trades_observed_count=max(trades_observed, len(rows)),
                positions_resolved=positions_resolved,
                maturity=maturity,
            )
            rebuilt_wallets += 1

        logger.info(
            f"Rebuilt decision process for {rebuilt_wallets} wallets "
            f"from {rebuilt_orders} observed leader orders"
        )
        return {"wallets": rebuilt_wallets, "orders": rebuilt_orders}

    async def _get_market_category(self, market_id: str) -> str:
        try:
            async with get_db() as conn:
                row = await conn.fetchrow(
                    "SELECT category FROM markets WHERE market_id = $1",
                    market_id,
                )
                if row and row["category"]:
                    return row["category"]
        except Exception as e:
            logger.debug(f"Market category lookup failed for {market_id}: {e}")
        return "unknown"

    async def _get_market_liquidity(self, market_id: str) -> float:
        try:
            async with get_db() as conn:
                row = await conn.fetchrow(
                    "SELECT liquidity_score FROM markets WHERE market_id = $1",
                    market_id,
                )
                if row and row["liquidity_score"] is not None:
                    return float(row["liquidity_score"])
        except Exception as e:
            logger.debug(f"Market liquidity lookup failed for {market_id}: {e}")
        return 0.5

    def _compose_backfill_trade_context(
        self,
        profile: dict,
        row: dict,
        maturity: float,
        confirmed_followers: int,
    ) -> dict:
        leader_context = dict(row.get("leader_context") or {})
        trade_context = dict(leader_context.get("trade_context") or {})

        category = trade_context.get("category") or row.get("category") or "unknown"
        liquidity_score = float(
            trade_context.get("liquidity_score")
            if trade_context.get("liquidity_score") is not None
            else (row.get("liquidity_score") or 0.5)
        )
        size_usdc = float(
            trade_context.get("size_usdc")
            if trade_context.get("size_usdc") is not None
            else (row.get("size_usdc") or 0.0)
        )
        market_price = float(
            trade_context.get("market_price")
            if trade_context.get("market_price") is not None
            else (row.get("entry_price") or 0.5)
        )
        is_contrarian = bool(trade_context.get("is_contrarian", False))
        ewma_size = float(profile.get("sizing", {}).get("ewma_size", 0.0) or 0.0)
        size_ratio = float(
            trade_context.get("size_ratio")
            if trade_context.get("size_ratio") is not None
            else (size_usdc / ewma_size if ewma_size > 0 and size_usdc > 0 else 1.0)
        )

        composed = {
            "category": category,
            "liquidity_score": liquidity_score,
            "size_usdc": size_usdc,
            "market_price": market_price,
            "is_contrarian": is_contrarian,
            "size_ratio": round(size_ratio, 4),
            "profile_maturity": float(
                trade_context.get("profile_maturity")
                if trade_context.get("profile_maturity") is not None
                else maturity
            ),
            "confirmed_followers": int(
                trade_context.get("confirmed_followers")
                if trade_context.get("confirmed_followers") is not None
                else confirmed_followers
            ),
            "positions_resolved": int(trade_context.get("positions_resolved", 0) or 0),
            "trades_observed": int(trade_context.get("trades_observed", 0) or 0),
        }

        if trade_context.get("deviation_score") is not None:
            composed["deviation_score"] = float(trade_context["deviation_score"])
        else:
            composed["deviation_score"] = round(
                self.get_deviation_score(
                    profile,
                    {
                        "category": category,
                        "size_usdc": size_usdc,
                        "is_contrarian": is_contrarian,
                    },
                ),
                4,
            )

        if trade_context.get("p_error") is not None:
            composed["p_error"] = float(trade_context["p_error"])
        if trade_context.get("error_confidence") is not None:
            composed["error_confidence"] = float(trade_context["error_confidence"])
        if trade_context.get("trade_source") is not None:
            composed["trade_source"] = trade_context.get("trade_source")
        if trade_context.get("trade_time") is not None:
            composed["trade_time"] = trade_context.get("trade_time")
        if trade_context.get("trade_age_s") is not None:
            composed["trade_age_s"] = float(trade_context.get("trade_age_s") or 0.0)
        if trade_context.get("live_candidate") is not None:
            composed["live_candidate"] = bool(trade_context.get("live_candidate"))
        process = profile.get("decision_process", _default_process_state())
        composed.update(
            {
                "process_score": round(float(process.get("process_score_ewma", 0.5) or 0.5), 4),
                "flip_rate": round(float(process.get("flip_rate", 0.0) or 0.0), 4),
                "scale_in_rate": round(float(process.get("scale_in_rate", 0.0) or 0.0), 4),
                "avg_interarrival_s": round(
                    float(process.get("avg_interarrival_s", 0.0) or 0.0), 2
                ),
                "interarrival_s": None,
                "flip_flag": False,
                "scale_in_flag": False,
            }
        )
        return composed


# ─── Pure helper functions (easy to unit-test) ────────────────────────────────


def _size_weight(size_usdc: float | None, ewma_size: float | None) -> float:
    """
    Convert a trade's USDC size into a Dirichlet/Beta increment in [0.5, 3.0].

    Why a sub-linear scaling: a $20k trade carries more conviction than a
    $200 one, but not 100× more — we don't want a single whale trade to
    dominate a leader's category preference. We use sqrt(size / ewma) to
    compress the dynamic range, then clamp into [0.5, 3.0]. Trades smaller
    than the leader's typical size still bump the prior at half-strength;
    abnormally large trades bump 3× at most. Passing size_usdc<=0 (e.g.
    when the field is missing) falls back to the legacy +1.0 increment.
    """
    if not size_usdc or size_usdc <= 0:
        return 1.0
    baseline = float(ewma_size or 0.0)
    if baseline <= 0:
        # First trade we see for this leader — neutral weight.
        return 1.0
    import math
    ratio = float(size_usdc) / baseline
    weight = math.sqrt(max(ratio, 0.01))
    return max(0.5, min(3.0, weight))


def _update_dirichlet(
    profile: dict,
    category: str,
    size_usdc: float | None = None,
) -> None:
    """Increment the Dirichlet count for a category, weighted by trade size.

    Larger trades carry more weight in the leader's category preferences.
    The weight is bounded (see ``_size_weight``) so a single whale doesn't
    overwhelm the prior.
    """
    cats = profile.setdefault("preferred_categories", {})
    ewma_size = (profile.get("sizing") or {}).get("ewma_size")
    weight = _size_weight(size_usdc, ewma_size)
    if category not in cats:
        cats[category] = {"alpha": [1.0 + weight]}  # prior 1 + first observation
    else:
        cats[category]["alpha"][0] = float(cats[category]["alpha"][0]) + weight


def _update_sizing(profile: dict, size_usdc: float) -> None:
    """Update EWMA and running average of position sizes."""
    sizing = profile.setdefault("sizing", {"ewma_size": 0.0, "avg_size": 0.0})
    lam = settings.EWMA_LAMBDA
    prev = sizing.get("ewma_size") or size_usdc
    sizing["ewma_size"] = lam * prev + (1 - lam) * size_usdc
    # Simple running avg approximation
    sizing["avg_size"] = (sizing.get("avg_size") or 0) * 0.9 + size_usdc * 0.1


def _update_entry_patterns(profile: dict, is_contrarian: bool) -> None:
    """Update contrarian/momentum rates."""
    patterns = profile.setdefault(
        "entry_patterns",
        {"contrarian_rate": 0.0, "momentum_rate": 0.0, "trades_count": 0},
    )
    n = patterns.get("trades_count", 0) + 1
    patterns["trades_count"] = n
    # Running mean
    prev_contrarian = patterns.get("contrarian_rate", 0.0)
    new_contrarian = (
        prev_contrarian + (1.0 - prev_contrarian) / n
        if is_contrarian
        else prev_contrarian * (n - 1) / n
    )
    patterns["contrarian_rate"] = new_contrarian
    patterns["momentum_rate"] = 1.0 - new_contrarian


def _update_accuracy(
    profile: dict,
    category: str,
    win: bool,
    size_usdc: float | None = None,
) -> None:
    """Update per-category Beta-Binomial accuracy, size-weighted.

    The Beta posterior on each category absorbs more "evidence" from
    larger trades — a $50k winning bet shifts confidence more than a
    $50 winning bet. ``wins``/``losses`` keep the integer trade counts
    (used for raw display); ``beta_a``/``beta_b`` carry the size-weighted
    pseudo-counts that drive Thompson sampling downstream.
    """
    acc = profile.setdefault("accuracy", {"overall": 0.0, "resolved_count": 0, "by_category": {}})
    by_cat = acc.setdefault("by_category", {})

    if category not in by_cat:
        by_cat[category] = {"wins": 0, "losses": 0, "beta_a": 1.0, "beta_b": 1.0}

    cat_acc = by_cat[category]
    ewma_size = (profile.get("sizing") or {}).get("ewma_size")
    weight = _size_weight(size_usdc, ewma_size)
    if win:
        cat_acc["wins"] += 1
        cat_acc["beta_a"] = float(cat_acc.get("beta_a", 1.0)) + weight
    else:
        cat_acc["losses"] += 1
        cat_acc["beta_b"] = float(cat_acc.get("beta_b", 1.0)) + weight

    # Update overall (raw, unweighted — kept for legacy display)
    total_wins = sum(c["wins"] for c in by_cat.values())
    total = total_wins + sum(c["losses"] for c in by_cat.values())
    acc["overall"] = total_wins / total if total > 0 else 0.0
    acc["resolved_count"] = total


def _compute_deviation_score(profile: dict, trade: dict) -> float:
    """
    Score how atypical a trade is relative to the leader's historical profile.
    Returns a value in [0, 1].
    """
    _ensure_profile_schema(profile)

    score = 0.0
    category = trade.get("category", "unknown")
    categories = profile.get("preferred_categories", {})
    if categories:
        top_categories = {
            key
            for key, _ in sorted(
                categories.items(),
                key=lambda item: float((item[1].get("alpha") or [1.0])[0]),
                reverse=True,
            )[:3]
        }
        if category not in top_categories:
            score += 0.4

    size_usdc = float(trade.get("size_usdc") or 0.0)
    ewma_size = float(profile.get("sizing", {}).get("ewma_size", 0.0) or 0.0)
    if ewma_size > 0 and size_usdc > 0:
        size_ratio = size_usdc / ewma_size
        if size_ratio > 3.0 or size_ratio < 0.3:
            score += 0.3

    is_contrarian = bool(trade.get("is_contrarian", False))
    contrarian_rate = float(profile.get("entry_patterns", {}).get("contrarian_rate", 0.0) or 0.0)
    momentum_rate = float(profile.get("entry_patterns", {}).get("momentum_rate", 0.0) or 0.0)
    if is_contrarian and contrarian_rate < 0.3:
        score += 0.3
    if not is_contrarian and momentum_rate < 0.3:
        score += 0.3

    return round(min(1.0, score), 4)


def _compute_maturity(positions_resolved: int, confirmed_followers: int) -> float:
    """Maturity = min(1, resolved/100) * min(1, followers/5)."""
    return min(1.0, positions_resolved / 100) * min(1.0, confirmed_followers / 5)


def _ensure_profile_schema(profile: dict) -> None:
    profile.setdefault("preferred_categories", {})
    profile.setdefault("sizing", {"ewma_size": 0.0, "avg_size": 0.0})
    profile.setdefault(
        "entry_patterns",
        {"contrarian_rate": 0.0, "momentum_rate": 0.0, "trades_count": 0},
    )
    profile.setdefault("accuracy", {"overall": 0.0, "resolved_count": 0, "by_category": {}})
    profile.setdefault("follower_impact", {"avg_volume_induced": 0.0, "avg_price_move": 0.0})
    process = profile.setdefault("decision_process", _default_process_state())
    defaults = _default_process_state()
    for key, value in defaults.items():
        process.setdefault(key, copy.deepcopy(value))
    _ensure_decision_learning(profile)
    loss_analysis = profile.setdefault(
        "loss_analysis",
        {"recent_losses": [], "last_position_loss_at": None},
    )
    loss_analysis.setdefault("recent_losses", [])
    loss_analysis.setdefault("last_position_loss_at", None)


def _ensure_decision_learning(profile: dict) -> dict:
    learning = profile.setdefault("decision_learning", {})
    for action in ("follow", "fade"):
        bucket = learning.get(action)
        if not isinstance(bucket, dict):
            learning[action] = _default_decision_bucket()
            continue
        defaults = _default_decision_bucket()
        for key, value in defaults.items():
            bucket.setdefault(key, copy.deepcopy(value))
    return learning


def _reset_decision_learning(profile: dict) -> None:
    profile["decision_learning"] = {
        "follow": _default_decision_bucket(),
        "fade": _default_decision_bucket(),
    }
    loss_analysis = profile.setdefault("loss_analysis", {})
    loss_analysis["recent_losses"] = []
    loss_analysis.setdefault("last_position_loss_at", None)


def _reset_order_process(profile: dict) -> None:
    profile["decision_process"] = _default_process_state()


def _running_average(previous: float, count: int, new_value: float) -> float:
    if count <= 1:
        return new_value
    return previous + (new_value - previous) / count


def _infer_reason_codes(
    profile: dict,
    action: str,
    trade_context: dict,
    confidence: float = 0.0,
    close_reason: str | None = None,
) -> list[str]:
    _ensure_profile_schema(profile)

    reason_codes: list[str] = []
    deviation_score = float(trade_context.get("deviation_score") or 0.0)
    size_ratio = float(trade_context.get("size_ratio") or 1.0)
    liquidity_score = float(trade_context.get("liquidity_score") or 0.5)
    market_price = float(trade_context.get("market_price") or 0.5)
    maturity = float(trade_context.get("profile_maturity") or 0.0)
    confirmed_followers = int(trade_context.get("confirmed_followers") or 0)
    p_error = float(trade_context.get("p_error") or 0.0)
    fade_confidence = float(trade_context.get("error_confidence") or confidence or 0.0)

    if close_reason:
        reason_codes.append(f"close_{close_reason}")
    if maturity < 0.35:
        reason_codes.append("low_profile_maturity")
    if deviation_score >= 0.6:
        reason_codes.append("high_deviation")
    if size_ratio >= 1.75:
        reason_codes.append("oversized_vs_profile")
    elif size_ratio <= 0.4:
        reason_codes.append("undersized_signal")
    if liquidity_score < 0.35:
        reason_codes.append("low_liquidity")
    if bool(trade_context.get("is_contrarian", False)):
        reason_codes.append("contrarian_entry")
    if market_price <= 0.15 or market_price >= 0.85:
        reason_codes.append("extreme_market_price")

    if action == "follow":
        from src.config import eff
        if confirmed_followers < eff("FOLLOW_MIN_FOLLOWERS") + 2:
            reason_codes.append("fragile_follow_consensus")
        if p_error >= 0.6:
            reason_codes.append("follow_against_error_model")
    elif action == "fade":
        from src.config import eff
        if fade_confidence < eff("FADE_MIN_CONFIDENCE") + 0.1:
            reason_codes.append("weak_fade_confidence")
        if p_error < 0.55:
            reason_codes.append("fade_without_edge")

    process_score = float(trade_context.get("process_score") or 0.0)
    if process_score and process_score < 0.4:
        reason_codes.append("unstable_order_process")
    if float(trade_context.get("flip_rate") or 0.0) > 0.3 or bool(
        trade_context.get("flip_flag", False)
    ):
        reason_codes.append("high_flip_rate")
    if float(trade_context.get("scale_in_rate") or 0.0) > 0.45 or bool(
        trade_context.get("scale_in_flag", False)
    ):
        reason_codes.append("aggressive_scale_in")
    interarrival_s = trade_context.get("interarrival_s")
    if interarrival_s is not None and float(interarrival_s) <= PROCESS_BURST_WINDOW_S:
        reason_codes.append("burst_trading")

    if not reason_codes:
        reason_codes.append("baseline_context")
    return sorted(set(reason_codes))


def _update_decision_process(profile: dict, trade: dict) -> None:
    _ensure_profile_schema(profile)
    process = profile["decision_process"]

    market_id = trade.get("market_id", "")
    side = (trade.get("side") or "").upper()
    category = trade.get("category", "unknown")
    size_usdc = float(trade.get("size_usdc") or 0.0)
    time_iso = trade.get("time")

    prev_orders = int(process.get("orders_seen", 0) or 0)
    new_orders = prev_orders + 1
    process["orders_seen"] = new_orders

    if side == "BUY":
        process["buy_count"] = int(process.get("buy_count", 0) or 0) + 1
    elif side == "SELL":
        process["sell_count"] = int(process.get("sell_count", 0) or 0) + 1

    prev_avg = float(process.get("avg_order_size", 0.0) or 0.0)
    process["avg_order_size"] = _running_average(prev_avg, new_orders, size_usdc)

    lam = settings.EWMA_LAMBDA
    prev_ewma = float(process.get("ewma_order_size", 0.0) or 0.0)
    ewma_base = prev_ewma if prev_ewma > 0 else size_usdc
    process["ewma_order_size"] = lam * ewma_base + (1 - lam) * size_usdc

    category_counts = process.setdefault("category_counts", {})
    known_category = category in category_counts
    category_counts[category] = int(category_counts.get(category, 0) or 0) + 1
    category_last_seen = process.setdefault("category_last_seen_at", {})
    category_last_seen[category] = time_iso or datetime.now(tz=timezone.utc).isoformat()

    score = 0.75
    if prev_ewma > 0 and size_usdc > 0:
        size_ratio = size_usdc / prev_ewma
        if size_ratio > 2.5 or size_ratio < 0.4:
            score -= 0.2
    if prev_orders >= 5 and not known_category:
        score -= 0.1

    last_order = process.get("last_order", {}) or {}
    transitions = int(process.get("transitions_seen", 0) or 0)
    interarrival_s = None
    flip_event = False
    scale_in_event = False
    if last_order:
        try:
            current_dt = (
                datetime.fromisoformat(time_iso.replace("Z", "+00:00")) if time_iso else None
            )
            last_dt = datetime.fromisoformat(last_order["time"].replace("Z", "+00:00"))
            if current_dt is not None:
                interarrival_s = max(0.0, (current_dt - last_dt).total_seconds())
        except (KeyError, TypeError, ValueError):
            interarrival_s = None

        same_market = market_id and market_id == last_order.get("market_id")
        same_side = side and side == last_order.get("side")
        if (
            same_market
            and same_side
            and interarrival_s is not None
            and interarrival_s <= PROCESS_SCALE_WINDOW_S
        ):
            scale_in_event = True
            score -= 0.15
        if (
            same_market
            and not same_side
            and interarrival_s is not None
            and interarrival_s <= PROCESS_SCALE_WINDOW_S
        ):
            flip_event = True
            score -= 0.25
        if interarrival_s is not None:
            avg_interarrival = float(process.get("avg_interarrival_s", 0.0) or 0.0)
            if interarrival_s <= PROCESS_BURST_WINDOW_S:
                score -= 0.2
            elif avg_interarrival > 0 and interarrival_s < avg_interarrival * 0.35:
                score -= 0.1

            transitions += 1
            process["transitions_seen"] = transitions
            process["avg_interarrival_s"] = (
                _running_average(avg_interarrival, transitions, interarrival_s)
                if avg_interarrival > 0
                else interarrival_s
            )
            process["flip_rate"] = _running_average(
                float(process.get("flip_rate", 0.0) or 0.0),
                transitions,
                1.0 if flip_event else 0.0,
            )
            process["scale_in_rate"] = _running_average(
                float(process.get("scale_in_rate", 0.0) or 0.0),
                transitions,
                1.0 if scale_in_event else 0.0,
            )

    score = max(0.0, min(1.0, score))
    prev_score = float(process.get("process_score_ewma", 0.5) or 0.5)
    process["process_score_ewma"] = lam * prev_score + (1 - lam) * score
    process["last_order"] = {
        "time": time_iso or datetime.now(tz=timezone.utc).isoformat(),
        "market_id": market_id,
        "side": side,
        "category": category,
        "size_usdc": size_usdc,
        "score": round(score, 4),
    }


def _reason_penalty_from_profile(profile: dict, action: str, reason_codes: list[str]) -> float:
    learning = _ensure_decision_learning(profile)
    bucket = learning.get(action, {})
    reason_stats = bucket.get("reason_stats", {})

    penalties: list[float] = []
    for code in reason_codes:
        stats = reason_stats.get(code)
        if not stats:
            continue
        total = float(stats.get("beta_a", 1.0)) + float(stats.get("beta_b", 1.0))
        samples = int(stats.get("wins", 0)) + int(stats.get("losses", 0))
        if total <= 0 or samples < 3:
            continue
        loss_rate = float(stats.get("beta_b", 1.0)) / total
        penalties.append(loss_rate)

    if not penalties:
        return 0.0
    return round(min(0.75, sum(penalties) / len(penalties)), 4)


def _update_decision_learning(
    profile: dict,
    action: str,
    won: bool,
    pnl_usdc: float,
    confidence: float,
    reason_codes: list[str],
    market_id: str,
    close_reason: str,
    event_time: str,
    trade_context: dict,
) -> None:
    learning = _ensure_decision_learning(profile)
    bucket = learning[action]

    if won:
        bucket["wins"] += 1
        bucket["beta_a"] += 1.0
        bucket["avg_win_pnl"] = _running_average(
            float(bucket.get("avg_win_pnl", 0.0)),
            int(bucket["wins"]),
            pnl_usdc,
        )
        bucket["avg_win_confidence"] = _running_average(
            float(bucket.get("avg_win_confidence", 0.0)),
            int(bucket["wins"]),
            confidence,
        )
    else:
        bucket["losses"] += 1
        bucket["beta_b"] += 1.0
        bucket["avg_loss_pnl"] = _running_average(
            float(bucket.get("avg_loss_pnl", 0.0)),
            int(bucket["losses"]),
            pnl_usdc,
        )
        bucket["avg_loss_confidence"] = _running_average(
            float(bucket.get("avg_loss_confidence", 0.0)),
            int(bucket["losses"]),
            confidence,
        )

    reason_stats = bucket.setdefault("reason_stats", {})
    for code in reason_codes:
        stats = reason_stats.setdefault(
            code,
            {"wins": 0, "losses": 0, "beta_a": 1.0, "beta_b": 1.0, "avg_pnl": 0.0},
        )
        if won:
            stats["wins"] += 1
            stats["beta_a"] += 1.0
            sample_count = int(stats["wins"])
        else:
            stats["losses"] += 1
            stats["beta_b"] += 1.0
            sample_count = int(stats["losses"])
        stats["avg_pnl"] = _running_average(
            float(stats.get("avg_pnl", 0.0)),
            sample_count,
            pnl_usdc,
        )

    if not won:
        loss_analysis = profile.setdefault("loss_analysis", {"recent_losses": []})
        recent_losses = loss_analysis.setdefault("recent_losses", [])
        recent_losses.insert(
            0,
            {
                "time": event_time,
                "action": action,
                "market_id": market_id,
                "close_reason": close_reason,
                "pnl_usdc": round(pnl_usdc, 2),
                "confidence": round(confidence, 4),
                "reason_codes": reason_codes,
                "trade_context": {
                    "category": trade_context.get("category", "unknown"),
                    "deviation_score": round(float(trade_context.get("deviation_score") or 0.0), 4),
                    "size_ratio": round(float(trade_context.get("size_ratio") or 1.0), 4),
                    "liquidity_score": round(float(trade_context.get("liquidity_score") or 0.5), 4),
                    "is_contrarian": bool(trade_context.get("is_contrarian", False)),
                    "profile_maturity": round(
                        float(trade_context.get("profile_maturity") or 0.0), 4
                    ),
                },
            },
        )
        del recent_losses[RECENT_LOSS_LIMIT:]


def _parse_event_time(value: str | None) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None


def _is_valid_learning_sample(
    trade_context: dict,
    close_reason: str | None = None,
    opened_at: str | datetime | None = None,
    closed_at: str | datetime | None = None,
) -> bool:
    if not isinstance(trade_context, dict):
        trade_context = {}

    if trade_context.get("live_candidate") is False:
        return False

    trade_age_s = trade_context.get("trade_age_s")
    try:
        trade_age_s = float(trade_age_s) if trade_age_s is not None else None
    except (TypeError, ValueError):
        trade_age_s = None
    if trade_age_s is not None and trade_age_s > float(settings.LIVE_DECISION_MAX_TRADE_AGE_S):
        return False

    opened_dt = _parse_event_time(opened_at)
    closed_dt = _parse_event_time(closed_at)
    if (
        close_reason == "market_resolved"
        and opened_dt is not None
        and closed_dt is not None
        and (closed_dt - opened_dt).total_seconds()
        <= float(settings.INVALID_LEARNING_CLOSE_WINDOW_S)
    ):
        return False

    return True


def _hours_between(current_time: str | None, previous_time: str | None) -> float | None:
    current_dt = _parse_event_time(current_time)
    previous_dt = _parse_event_time(previous_time)
    if current_dt is None or previous_dt is None:
        return None
    return max(0.0, (current_dt - previous_dt).total_seconds() / 3600.0)


def _hours_since_position_loss(profile: dict, event_time: str | None) -> float | None:
    _ensure_profile_schema(profile)
    last_loss_at = profile.get("loss_analysis", {}).get("last_position_loss_at")
    return _hours_between(event_time, last_loss_at)


def _hours_since_category_trade(
    profile: dict, category: str, event_time: str | None
) -> float | None:
    _ensure_profile_schema(profile)
    category_last_seen = profile.get("decision_process", {}).get("category_last_seen_at", {})
    last_seen = category_last_seen.get(category)
    return _hours_between(event_time, last_seen)


def _get_category_accuracy(profile: dict, category: str) -> float:
    _ensure_profile_schema(profile)
    by_category = profile.get("accuracy", {}).get("by_category", {})
    if category not in by_category:
        return 0.5
    bucket = by_category[category]
    wins = float(bucket.get("wins", 0) or 0.0)
    losses = float(bucket.get("losses", 0) or 0.0)
    total = wins + losses
    if total <= 0:
        return 0.5
    return wins / total


def _cyclical_time_features(event_time: str | None) -> dict:
    event_dt = _parse_event_time(event_time) or datetime.now(tz=timezone.utc)
    hour = event_dt.hour + event_dt.minute / 60.0
    dow = float(event_dt.weekday())
    hour_angle = 2.0 * math.pi * hour / 24.0
    dow_angle = 2.0 * math.pi * dow / 7.0
    return {
        "hour_sin": round(float(math.sin(hour_angle)), 6),
        "hour_cos": round(float(math.cos(hour_angle)), 6),
        "dow_sin": round(float(math.sin(dow_angle)), 6),
        "dow_cos": round(float(math.cos(dow_angle)), 6),
    }


def _compute_process_insights(profile: dict, trade: dict) -> dict:
    _ensure_profile_schema(profile)
    process = profile.get("decision_process", _default_process_state())
    last_order = process.get("last_order", {}) or {}

    size_usdc = float(trade.get("size_usdc") or 0.0)
    category = trade.get("category", "unknown")
    market_id = trade.get("market_id", "")
    side = (trade.get("side") or "").upper()
    score = float(process.get("process_score_ewma", 0.5) or 0.5)

    old_ewma = float(process.get("ewma_order_size", 0.0) or 0.0)
    size_ratio = size_usdc / old_ewma if old_ewma > 0 and size_usdc > 0 else 1.0
    if old_ewma > 0 and (size_ratio > 2.5 or size_ratio < 0.4):
        score -= 0.2

    category_counts = process.get("category_counts", {})
    if int(process.get("orders_seen", 0) or 0) >= 5 and category not in category_counts:
        score -= 0.1

    interarrival_s = None
    hours_since_last_trade = None
    flip_flag = False
    scale_in_flag = False
    if last_order:
        try:
            current_time = trade.get("time")
            if isinstance(current_time, str):
                now_dt = datetime.fromisoformat(current_time.replace("Z", "+00:00"))
            else:
                now_dt = datetime.now(tz=timezone.utc)
            last_dt = datetime.fromisoformat(last_order["time"].replace("Z", "+00:00"))
            interarrival_s = max(0.0, (now_dt - last_dt).total_seconds())
            hours_since_last_trade = round(interarrival_s / 3600.0, 4)
        except (KeyError, TypeError, ValueError):
            interarrival_s = None
            hours_since_last_trade = None

        same_market = market_id and market_id == last_order.get("market_id")
        same_side = side and side == last_order.get("side")
        if (
            same_market
            and same_side
            and interarrival_s is not None
            and interarrival_s <= PROCESS_SCALE_WINDOW_S
        ):
            scale_in_flag = True
            score -= 0.15
        if (
            same_market
            and not same_side
            and interarrival_s is not None
            and interarrival_s <= PROCESS_SCALE_WINDOW_S
        ):
            flip_flag = True
            score -= 0.25

        avg_interarrival = float(process.get("avg_interarrival_s", 0.0) or 0.0)
        if interarrival_s is not None:
            if interarrival_s <= PROCESS_BURST_WINDOW_S:
                score -= 0.2
            elif avg_interarrival > 0 and interarrival_s < avg_interarrival * 0.35:
                score -= 0.1

    return {
        "process_score": round(max(0.0, min(1.0, score)), 4),
        "flip_rate": round(float(process.get("flip_rate", 0.0) or 0.0), 4),
        "scale_in_rate": round(float(process.get("scale_in_rate", 0.0) or 0.0), 4),
        "avg_interarrival_s": round(float(process.get("avg_interarrival_s", 0.0) or 0.0), 2),
        "interarrival_s": round(interarrival_s, 2) if interarrival_s is not None else None,
        "hours_since_last_trade": hours_since_last_trade,
        "flip_flag": flip_flag,
        "scale_in_flag": scale_in_flag,
    }
