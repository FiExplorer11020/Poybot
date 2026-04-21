"""
Trade Observer — dual-source trade ingestion: WebSocket + data-api.polymarket.com backfill.
Deduplicates trades using Redis. Stores to trades_observed. Publishes to Redis pub/sub.

Note: Polymarket CLOB WebSocket market channel sends orderbook/price_change events only
(no wallet addresses). Leader trade attribution comes exclusively from data-api backfill.
"""

import asyncio
import hashlib
import json
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import aiohttp
from loguru import logger

from src.config import settings
from src.database.connection import get_db
from src.observer.websocket_client import PolymarketWSClient
from src.registry.falcon_client import FalconClient

REDIS_TRADES_CHANNEL = "trades:observed"
DEDUP_KEY_PREFIX = "seen_trades"
DEDUP_TTL_S = 7 * 86400  # 7 days
MARKET_META_TTL_S = 3600
SOURCE_API_WALLET = "api_wallet"
SOURCE_API_MARKET = "api_market"


def _row_value(row: Any, key: str, default: Any = None) -> Any:
    if row is None:
        return default
    try:
        return row[key]
    except Exception:
        return default


def _json_dict(raw: Any) -> dict:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            return {}
    if raw is None:
        return {}
    try:
        return dict(raw)
    except Exception:
        return {}


def _market_type_label(category: Any, question: Any = None) -> str:
    category_text = str(category or "").strip()
    text = f"{category_text} {question or ''}".lower()
    sports_tokens = (
        " vs ",
        " o/u ",
        "map ",
        "set ",
        "grand prix",
        "premier league",
        "champions league",
        "world cup",
        "tennis",
        "soccer",
        "football",
        "nba",
        "nfl",
        "mlb",
        "nhl",
        "cup",
        "fc",
        "winner",
        " win on 20",
    )
    crypto_tokens = ("bitcoin", "btc", "ethereum", "eth", "crypto", "solana", "xrp")
    if any(token in text for token in crypto_tokens):
        return "crypto"
    if any(token in text for token in sports_tokens):
        return "sports"
    politics_tokens = ("election", "president", "senate", "vote", "parliament", "mayor")
    if any(token in text for token in politics_tokens):
        return "politics"
    if any(token in text for token in ("fed", "inflation", "cpi", "rate cut", "recession", "gdp")):
        return "macro"
    if any(token in text for token in ("movie", "album", "oscar", "grammy", "tv", "show")):
        return "entertainment"
    if category_text and category_text.lower() != "unknown":
        return category_text

    return "unknown"


def _infer_market_category(question: Any = None, slug: Any = None) -> str:
    return _market_type_label(None, f"{question or ''} {slug or ''}")


def _normalize_token_ids(raw: Any) -> list[str]:
    tokens = raw
    try:
        if isinstance(tokens, str):
            tokens = json.loads(tokens)
    except Exception:
        tokens = [tokens]
    if not isinstance(tokens, list):
        tokens = [tokens]
    return [str(token) for token in tokens if token]


def _token_hints_from_trade(
    token_id: str,
    outcome_index: Any = None,
    outcome_name: Any = None,
) -> tuple[str | None, str | None]:
    if not token_id:
        return None, None
    try:
        idx = int(outcome_index) if outcome_index is not None else None
    except (TypeError, ValueError):
        idx = None
    outcome = str(outcome_name or "").strip().lower()
    if idx == 0:
        return token_id, None
    if idx == 1:
        return None, token_id
    if outcome in {"yes", "up"}:
        return token_id, None
    if outcome in {"no", "down"}:
        return None, token_id
    return None, None


def _gamma_market_matches_request(market: dict, market_id: str, token_id: str) -> bool:
    condition_id = str(market.get("conditionId") or market.get("condition_id") or "").strip()
    if market_id and condition_id and condition_id != market_id:
        return False

    tokens = set(_normalize_token_ids(market.get("clobTokenIds")))
    single_token = str(market.get("clobTokenId") or "").strip()
    if single_token:
        tokens.add(single_token)
    if token_id and tokens and token_id not in tokens:
        return False

    if market_id and not condition_id and not tokens:
        return False
    return True


class TradeObserver:
    def __init__(
        self,
        falcon_client: FalconClient,
        redis_client,  # redis.asyncio.Redis
        leader_wallets: set[str] | None = None,
        leader_markets: set[str] | None = None,
    ):
        self._falcon = falcon_client
        self._redis = redis_client
        self._leader_wallets: set[str] = leader_wallets or set()
        self._leader_markets: set[str] = leader_markets or set()
        self._leader_condition_ids: set[str] = set()
        self._running = False
        self._stop_event = asyncio.Event()
        self._ws_client: PolymarketWSClient | None = None
        self._inserted: int = 0
        self._market_meta_cache: dict[str, float] = {}
        self._book_age_samples: deque[float] = deque(maxlen=512)

    @property
    def inserted_count(self) -> int:
        return self._inserted

    def update_leaders(self, wallets: set[str], markets: set[str]) -> None:
        """Dynamically update leader wallets and markets."""
        self._leader_wallets = wallets
        self._leader_markets = markets
        if self._ws_client:
            self._ws_client.update_markets(markets)

    async def start(self) -> None:
        self._running = True
        self._stop_event.clear()
        self._ws_client = PolymarketWSClient(
            on_message=self._handle_ws_message,
            markets=self._leader_markets,
        )
        tasks = [
            asyncio.create_task(self._ws_client.start()),
            asyncio.create_task(self._backfill_loop()),
        ]
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            pass

    async def stop(self) -> None:
        self._running = False
        self._stop_event.set()
        if self._ws_client:
            await self._ws_client.stop()

    async def _handle_ws_message(self, msg: dict) -> None:
        """Process a single WebSocket market message.

        The CLOB market channel sends orderbook snapshots (event_type='book') and
        price_change events. Neither includes wallet addresses. We log price changes
        to Redis for market monitoring and ignore the rest.
        """
        event_type = msg.get("event_type", "")
        if self._redis:
            try:
                await self._redis.set("ws:market:last_message_ts", str(time.time()), ex=300)
            except Exception:
                pass

        if event_type == "trade":
            await self._process_legacy_ws_trade(msg)
        elif event_type == "price_change":
            market_id = msg.get("market", "")
            changes = msg.get("price_changes", [])
            if market_id and changes and self._redis:
                try:
                    await self._redis.publish(
                        "market:price_changes",
                        json.dumps(
                            {
                                "market": market_id,
                                "changes": changes,
                                "ts": msg.get("timestamp"),
                            }
                        ),
                    )
                except Exception:
                    pass
                # FIX 7: Cache latest price per token in Redis (300s TTL)
                for change in changes:
                    token_id = change.get("asset_id", "")
                    price = change.get("price")
                    if token_id and price is not None:
                        try:
                            await self._redis.setex(
                                f"price:{market_id}:{token_id}", 300, str(price)
                            )
                        except Exception:
                            pass
        elif event_type == "book":
            await self._record_book_metrics(msg)

    @staticmethod
    def _event_timestamp_s(raw_ts: Any) -> float | None:
        if raw_ts is None:
            return None
        try:
            ts = float(raw_ts)
        except (TypeError, ValueError):
            return None
        if ts > 10_000_000_000:
            ts /= 1000.0
        return ts

    @staticmethod
    def _percentile(values: list[float], pct: float) -> float:
        if not values:
            return 0.0
        ordered = sorted(values)
        idx = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * pct)))
        return ordered[idx]

    async def _record_book_metrics(self, msg: dict) -> None:
        if not self._redis:
            return
        now_s = time.time()
        ts_s = self._event_timestamp_s(msg.get("timestamp") or msg.get("time") or msg.get("ts"))
        age_s = 0.0 if ts_s is None else max(0.0, now_s - ts_s)
        self._book_age_samples.append(age_s)
        p95_s = self._percentile(list(self._book_age_samples), 0.95)
        try:
            await self._redis.setex("metrics:book_age_p95_s", 300, f"{p95_s:.3f}")
            market_id = str(msg.get("market") or msg.get("market_id") or "")
            token_id = str(msg.get("asset_id") or msg.get("token_id") or msg.get("asset") or "")
            if market_id and token_id:
                await self._redis.setex(
                    f"book:last:{market_id}:{token_id}",
                    300,
                    json.dumps(
                        {
                            "market_id": market_id,
                            "token_id": token_id,
                            "age_s": round(age_s, 3),
                            "book_age_p95_s": round(p95_s, 3),
                            "observed_ts": now_s,
                            "source_timestamp": msg.get("timestamp")
                            or msg.get("time")
                            or msg.get("ts"),
                            "bid_levels": len(msg.get("bids") or []),
                            "ask_levels": len(msg.get("asks") or []),
                        }
                    ),
                )
        except Exception:
            logger.debug("Failed to update book quality Redis metrics", exc_info=True)

    async def _process_legacy_ws_trade(self, msg: dict) -> None:
        """Handle legacy trade-shaped WS events when wallet attribution is present.

        The current market channel is primarily book/price data, but older tests and
        some feeds can still emit trade-shaped payloads. We accept them only when a
        wallet address is present.
        """
        maker = str(msg.get("maker_address") or "")
        taker = str(msg.get("taker_address") or "")
        wallet = maker if maker in self._leader_wallets else taker
        if not wallet:
            wallet = maker or taker
        if not wallet:
            return

        try:
            market_id = str(msg.get("market") or msg.get("market_id") or "")
            token_id = str(msg.get("asset_id") or msg.get("token_id") or msg.get("asset") or "")
            side = str(msg.get("side") or "").upper()
            price = Decimal(str(msg.get("price", 0)))
            size_shares = Decimal(str(msg.get("size", 0)))
            size_usdc = (size_shares * price).quantize(Decimal("0.01"))
            ts_int = int(msg.get("timestamp", 0))
            trade_time = datetime.fromtimestamp(
                ts_int / 1000 if ts_int > 1_000_000_000_000 else ts_int,
                tz=timezone.utc,
            )
        except (ValueError, TypeError) as exc:
            logger.debug(f"Bad legacy WS trade: {exc} | msg={msg}")
            return

        await self._process_trade(
            market_id=market_id,
            token_id=token_id,
            wallet_address=wallet,
            side=side,
            price=price,
            size_usdc=size_usdc,
            trade_time=trade_time,
            source="websocket",
        )

    async def _backfill_loop(self) -> None:
        """Poll data-api.polymarket.com every 60s for leader trades."""
        while self._running and not self._stop_event.is_set():
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=max(5, int(settings.TRADE_OBSERVER_POLL_INTERVAL_S)),
                )
                break
            except asyncio.TimeoutError:
                pass
            if not self._running:
                break
            await self._backfill_from_data_api()

    async def _backfill_from_data_api(self) -> None:
        """Fetch leader and market activity from data-api.polymarket.com."""
        if not self._leader_wallets:
            return
        wallet_records = 0
        market_records = 0
        async with aiohttp.ClientSession() as session:
            wallet_records = await self._backfill_wallet_trades(session)
            market_records = await self._backfill_market_activity(session)
        total = wallet_records + market_records
        if total:
            logger.debug(
                "data-api backfill: processed "
                f"{wallet_records} leader-wallet trades + {market_records} market trades"
            )

    async def _backfill_from_falcon(self) -> None:
        """Compatibility backfill for older Falcon trade fixtures."""
        for wallet in list(self._leader_wallets):
            try:
                trades = await self._falcon.query(
                    556,
                    {"wallet_proxy": wallet},
                    limit=100,
                )
            except TypeError:
                trades = await self._falcon.query(556, {"wallet_proxy": wallet})
            except Exception as exc:
                logger.debug(f"Falcon compatibility backfill failed for {wallet}: {exc}")
                continue
            for trade in trades or []:
                await self._process_falcon_trade(trade, wallet)

    async def _process_falcon_trade(self, trade: dict, wallet_address: str) -> None:
        """Compatibility parser for Falcon trade rows used by legacy tests."""
        try:
            market_id = str(trade.get("market_id") or trade.get("condition_id") or "")
            token_id = str(trade.get("token_id") or trade.get("asset") or "")
            side = str(trade.get("side") or "").upper()
            price = Decimal(str(trade.get("price", 0)))
            size_shares = Decimal(str(trade.get("size", 0)))
            size_usdc = (size_shares * price).quantize(Decimal("0.01"))
            ts_int = int(trade.get("timestamp", 0))
            trade_time = datetime.fromtimestamp(
                ts_int / 1000 if ts_int > 1_000_000_000_000 else ts_int,
                tz=timezone.utc,
            )
        except (ValueError, TypeError) as exc:
            logger.debug(f"Bad Falcon compatibility trade: {exc} | trade={trade}")
            return

        await self._process_trade(
            market_id=market_id,
            token_id=token_id,
            wallet_address=wallet_address,
            side=side,
            price=price,
            size_usdc=size_usdc,
            trade_time=trade_time,
            source="falcon",
            market_question_hint=trade.get("title") or trade.get("slug"),
            market_slug_hint=trade.get("slug"),
            outcome_hint=trade.get("outcome"),
            outcome_index=trade.get("outcome_index"),
        )

    async def _backfill_wallet_trades(self, session: aiohttp.ClientSession) -> int:
        processed = 0
        for wallet in list(self._leader_wallets):
            if not self._running:
                break
            url = f"https://data-api.polymarket.com/trades?user={wallet}&limit=100"
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                    if resp.status != 200:
                        continue
                    trades = await resp.json()
                    for trade in trades:
                        await self._process_data_api_trade(trade, source=SOURCE_API_WALLET)
                        processed += 1
            except Exception as e:
                logger.debug(f"data-api wallet backfill failed for {wallet}: {e}")
        return processed

    async def _backfill_market_activity(self, session: aiohttp.ClientSession) -> int:
        target_markets = await self._get_recent_leader_market_ids()
        if not target_markets:
            return 0

        processed = 0
        url = (
            "https://data-api.polymarket.com/trades"
            f"?limit={max(50, int(settings.DATA_API_GLOBAL_TRADES_LIMIT))}"
        )
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return 0
                trades = await resp.json()
        except Exception as exc:
            logger.debug(f"data-api market activity fetch failed: {exc}")
            return 0

        for trade in trades:
            market_id = str(trade.get("conditionId") or "")
            if not market_id or market_id not in target_markets:
                continue
            await self._process_data_api_trade(trade, source=SOURCE_API_MARKET)
            processed += 1
        return processed

    async def _get_recent_leader_market_ids(self) -> set[str]:
        if self._leader_condition_ids:
            try:
                async with get_db() as conn:
                    rows = await conn.fetch(
                        """
                        SELECT market_id
                        FROM (
                            SELECT market_id, MAX(time) AS last_seen
                            FROM trades_observed
                            WHERE is_leader = TRUE
                            GROUP BY market_id
                            ORDER BY last_seen DESC
                            LIMIT $1
                        ) recent
                        """,
                        max(25, int(settings.DATA_API_RECENT_LEADER_MARKETS)),
                    )
                    recent = {str(r["market_id"]) for r in rows if r["market_id"]}
                    if recent:
                        self._leader_condition_ids.update(recent)
            except Exception as exc:
                logger.debug(f"Recent leader market lookup failed: {exc}")
            return set(self._leader_condition_ids)

        try:
            async with get_db() as conn:
                rows = await conn.fetch(
                    """
                    SELECT market_id
                    FROM (
                        SELECT market_id, MAX(time) AS last_seen
                        FROM trades_observed
                        WHERE is_leader = TRUE
                        GROUP BY market_id
                        ORDER BY last_seen DESC
                        LIMIT $1
                    ) recent
                    """,
                    max(25, int(settings.DATA_API_RECENT_LEADER_MARKETS)),
                )
                self._leader_condition_ids = {str(r["market_id"]) for r in rows if r["market_id"]}
        except Exception as exc:
            logger.debug(f"Leader market bootstrap failed: {exc}")
        return set(self._leader_condition_ids)

    async def _process_data_api_trade(self, trade: dict, source: str = SOURCE_API_WALLET) -> None:
        """Parse and store a trade from data-api.polymarket.com.

        Response shape:
          {proxyWallet, side, asset (token_id), conditionId (market_id),
           size (shares), price, timestamp (seconds or ms)}
        """
        try:
            wallet = trade.get("proxyWallet", "")
            if not wallet:
                return
            market_id = trade.get("conditionId", "")
            token_id = trade.get("asset", "")
            side = (trade.get("side") or "").upper()
            price = Decimal(str(trade.get("price", 0)))
            size_shares = float(trade.get("size", 0))
            size_usdc = Decimal(str(round(size_shares * float(price), 2)))
            ts_raw = trade.get("timestamp", 0)
            ts_int = int(ts_raw)
            # If > 1e10 it's milliseconds, otherwise seconds
            trade_time = datetime.fromtimestamp(
                ts_int / 1000 if ts_int > 1_000_000_000_000 else ts_int,
                tz=timezone.utc,
            )
        except (ValueError, TypeError) as e:
            logger.debug(f"Bad data-api trade: {e} | trade={trade}")
            return

        await self._process_trade(
            market_id=market_id,
            token_id=token_id,
            wallet_address=wallet,
            side=side,
            price=price,
            size_usdc=size_usdc,
            trade_time=trade_time,
            source=source,
            market_question_hint=trade.get("title"),
            market_slug_hint=trade.get("slug") or trade.get("eventSlug"),
            outcome_hint=trade.get("outcome"),
            outcome_index=trade.get("outcomeIndex"),
        )

    def _dedup_key(
        self,
        wallet: str,
        market_id: str,
        trade_time: datetime,
        side: str,
        price: Decimal,
        size_usdc: Decimal,
    ) -> str:
        day = trade_time.strftime("%Y%m%d")
        bucket = int(trade_time.timestamp() // 1)  # 1-second buckets
        raw = f"{bucket}:{side}:{price}:{size_usdc}"
        h = hashlib.md5(raw.encode()).hexdigest()[:12]
        return f"{DEDUP_KEY_PREFIX}:{wallet}:{market_id}:{day}:{h}"

    async def _is_duplicate(self, key: str) -> bool:
        result = await self._redis.set(key, "1", ex=DEDUP_TTL_S, nx=True)
        return result is None  # nx=True returns None if key already exists

    async def _clear_dedup_key(self, key: str) -> None:
        deleter = getattr(self._redis, "delete", None)
        if not callable(deleter):
            return
        try:
            await deleter(key)
        except Exception:
            pass

    async def _trade_exists(
        self,
        market_id: str,
        wallet_address: str,
        trade_time: datetime,
        side: str,
        price: Decimal,
        size_usdc: Decimal,
    ) -> bool:
        try:
            async with get_db() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT 1
                    FROM trades_observed
                    WHERE market_id = $1
                      AND wallet_address = $2
                      AND time = $3
                      AND side = $4
                      AND price = $5
                      AND size_usdc = $6
                    LIMIT 1
                    """,
                    market_id,
                    wallet_address,
                    trade_time,
                    side,
                    price,
                    size_usdc,
                )
                return row is not None
        except Exception:
            return False

    async def _process_trade(
        self,
        market_id: str,
        token_id: str,
        wallet_address: str,
        side: str,
        price: Decimal,
        size_usdc: Decimal,
        trade_time: datetime,
        source: str,
        market_question_hint: str | None = None,
        market_slug_hint: str | None = None,
        outcome_hint: str | None = None,
        outcome_index: int | None = None,
    ) -> None:
        """Deduplicate and store a trade, then publish to Redis."""
        if not market_id or not wallet_address:
            return

        dedup_key = self._dedup_key(wallet_address, market_id, trade_time, side, price, size_usdc)
        if await self._is_duplicate(dedup_key):
            if source == SOURCE_API_MARKET and not await self._trade_exists(
                market_id=market_id,
                wallet_address=wallet_address,
                trade_time=trade_time,
                side=side,
                price=price,
                size_usdc=size_usdc,
            ):
                await self._clear_dedup_key(dedup_key)
            else:
                return

        is_leader = wallet_address in self._leader_wallets
        if is_leader:
            self._leader_condition_ids.add(market_id)
        market_row = None
        leader_row = None

        try:
            async with get_db() as conn:
                await conn.execute(
                    """
                    INSERT INTO trades_observed
                        (
                            time, market_id, token_id, wallet_address, side, price,
                            size_usdc, source, is_leader
                        )
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                    """,
                    trade_time,
                    market_id,
                    token_id,
                    wallet_address,
                    side,
                    price,
                    size_usdc,
                    source,
                    is_leader,
                )
                self._inserted += 1
                # FIX 1: Ensure market stub exists so LEFT JOINs return a row
                await conn.execute(
                    """
                    INSERT INTO markets (market_id, question, category)
                    VALUES ($1, $2, 'unknown')
                    ON CONFLICT (market_id) DO NOTHING
                    """,
                    market_id,
                    market_question_hint or f"Market {market_id[:30]}…",
                )
                market_row = await conn.fetchrow(
                    """
                    SELECT question, category, token_yes, token_no, end_date
                    FROM markets
                    WHERE market_id = $1
                    """,
                    market_id,
                )
                market_row = await self._repair_market_from_trade_hint(
                    conn=conn,
                    market_id=market_id,
                    token_id=token_id,
                    trade_time=trade_time,
                    market_row=market_row,
                    market_question_hint=market_question_hint,
                    market_slug_hint=market_slug_hint,
                    outcome_hint=outcome_hint,
                    outcome_index=outcome_index,
                )
                if is_leader:
                    leader_row = await conn.fetchrow(
                        """
                        SELECT classification_json, excluded, on_watchlist
                        FROM leaders
                        WHERE wallet_address = $1
                        """,
                        wallet_address,
                    )
        except Exception as e:
            await self._clear_dedup_key(dedup_key)
            logger.error(f"Failed to insert trade: {e}")
            return

        if self._needs_market_enrichment(market_id, market_row):
            try:
                enriched = await self._fetch_market_metadata_from_gamma(market_id, token_id)
            except Exception as exc:
                logger.debug(f"Gamma market lookup failed for {market_id}: {exc}")
                enriched = None
            if enriched:
                try:
                    async with get_db() as conn:
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
                                volume_24h     = COALESCE(EXCLUDED.volume_24h, markets.volume_24h),
                                liquidity_score= COALESCE(
                                    EXCLUDED.liquidity_score,
                                    markets.liquidity_score
                                ),
                                fee_rate_pct   = COALESCE(
                                    EXCLUDED.fee_rate_pct,
                                    markets.fee_rate_pct
                                ),
                                updated_at     = NOW()
                            """,
                            market_id,
                            enriched["question"],
                            enriched["category"],
                            enriched["token_yes"],
                            enriched["token_no"],
                            enriched["end_date"],
                            enriched["volume_24h"],
                            enriched["liquidity_score"],
                            enriched["fee_rate_pct"],
                        )
                        market_row = {
                            "question": enriched["question"],
                            "category": enriched["category"],
                        }
                        self._market_meta_cache[market_id] = datetime.now(
                            tz=timezone.utc
                        ).timestamp()
                except Exception as exc:
                    logger.debug(f"Failed to upsert Gamma market metadata for {market_id}: {exc}")

        classification = _json_dict(_row_value(leader_row, "classification_json", {}))
        market_question = (
            _row_value(market_row, "question")
            or market_question_hint
            or f"Market {market_id[:30]}…"
        )
        market_category = _row_value(market_row, "category") or "unknown"
        market_type = _market_type_label(market_category, market_question)
        wallet_status = "market_participant"
        if is_leader:
            if bool(_row_value(leader_row, "excluded", False)):
                wallet_status = "excluded"
            elif bool(_row_value(leader_row, "on_watchlist", False)):
                wallet_status = "active"
            else:
                wallet_status = "watching"

        # Publish to Redis pub/sub
        event = {
            "time": trade_time.isoformat(),
            "market_id": market_id,
            "market_question": market_question,
            "market_category": market_category,
            "market_type": market_type,
            "token_id": token_id,
            "wallet_address": wallet_address,
            "wallet_type": "leader" if is_leader else "market_participant",
            "wallet_status": wallet_status,
            "wallet_strategy": classification.get("strategy"),
            "wallet_horizon": classification.get("horizon"),
            "wallet_influence": classification.get("influence"),
            "side": side,
            "price": str(price),
            "size_usdc": str(size_usdc),
            "is_leader": is_leader,
            "source": source,
        }
        try:
            await self._redis.publish(REDIS_TRADES_CHANNEL, json.dumps(event))
        except Exception as e:
            logger.warning(f"Failed to publish trade event: {e}")

    async def _repair_market_from_trade_hint(
        self,
        conn,
        market_id: str,
        token_id: str,
        trade_time: datetime,
        market_row: Any,
        market_question_hint: str | None = None,
        market_slug_hint: str | None = None,
        outcome_hint: str | None = None,
        outcome_index: int | None = None,
    ) -> dict:
        row = {
            "question": _row_value(market_row, "question"),
            "category": _row_value(market_row, "category"),
            "token_yes": _row_value(market_row, "token_yes"),
            "token_no": _row_value(market_row, "token_no"),
            "end_date": _row_value(market_row, "end_date"),
        }
        if not market_question_hint and outcome_index is None and not outcome_hint:
            return row

        question_hint = str(market_question_hint or "").strip()
        current_question = str(row.get("question") or "").strip()
        current_category = str(row.get("category") or "").strip() or "unknown"
        current_end_date = row.get("end_date")
        inferred_category = _infer_market_category(
            question_hint or current_question,
            market_slug_hint,
        )
        token_yes_hint, token_no_hint = _token_hints_from_trade(
            token_id=token_id,
            outcome_index=outcome_index,
            outcome_name=outcome_hint,
        )

        stale_end_date = bool(
            current_end_date and trade_time > current_end_date + timedelta(minutes=5)
        )
        should_refresh_question = bool(
            question_hint
            and (
                not current_question
                or current_question.startswith("Market ")
                or current_question != question_hint
            )
        )
        should_refresh_category = inferred_category != "unknown" and (
            current_category.lower() in {"", "unknown", "none", "null"}
            or stale_end_date
            or should_refresh_question
        )
        should_refresh_tokens = bool(
            (token_yes_hint and row.get("token_yes") != token_yes_hint)
            or (token_no_hint and row.get("token_no") != token_no_hint)
        )

        should_skip_refresh = not any(
            (
                should_refresh_question,
                should_refresh_category,
                should_refresh_tokens,
                stale_end_date,
            )
        )
        if should_skip_refresh:
            return row

        question_value = question_hint or current_question or f"Market {market_id[:30]}…"
        category_value = (
            inferred_category if should_refresh_category else current_category or "unknown"
        )
        token_yes_value = token_yes_hint or row.get("token_yes")
        token_no_value = token_no_hint or row.get("token_no")

        await conn.execute(
            """
            INSERT INTO markets (
                market_id, question, category, token_yes, token_no, end_date, updated_at
            )
            VALUES ($1, $2, $3, $4, $5, NULL, NOW())
            ON CONFLICT (market_id) DO UPDATE SET
                question = EXCLUDED.question,
                category = CASE
                    WHEN EXCLUDED.category IS NOT NULL AND EXCLUDED.category <> 'unknown'
                    THEN EXCLUDED.category
                    ELSE markets.category
                END,
                token_yes = COALESCE(EXCLUDED.token_yes, markets.token_yes),
                token_no = COALESCE(EXCLUDED.token_no, markets.token_no),
                end_date = CASE WHEN $6 THEN NULL ELSE markets.end_date END,
                updated_at = NOW()
            """,
            market_id,
            question_value,
            category_value,
            token_yes_value,
            token_no_value,
            stale_end_date,
        )
        self._market_meta_cache[market_id] = datetime.now(tz=timezone.utc).timestamp()
        return {
            "question": question_value,
            "category": category_value,
            "token_yes": token_yes_value,
            "token_no": token_no_value,
            "end_date": None if stale_end_date else current_end_date,
        }

    def _needs_market_enrichment(self, market_id: str, market_row: Any) -> bool:
        question = str(_row_value(market_row, "question", "") or "").strip()
        category = str(_row_value(market_row, "category", "") or "").strip().lower()
        now_ts = datetime.now(tz=timezone.utc).timestamp()
        last_fetch = float(self._market_meta_cache.get(market_id, 0.0) or 0.0)
        if now_ts - last_fetch < MARKET_META_TTL_S:
            return False
        if not question or question.startswith("Market "):
            return True
        return category in {"", "unknown", "none", "null"}

    async def _fetch_market_metadata_from_gamma(self, market_id: str, token_id: str) -> dict | None:
        url = "https://gamma-api.polymarket.com/markets"
        params_options = [{"conditionId": market_id, "limit": 1}]
        if token_id:
            params_options.append({"clobTokenIds": token_id, "limit": 1})
        async with aiohttp.ClientSession() as session:
            for params in params_options:
                async with session.get(
                    url,
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=8),
                ) as resp:
                    if resp.status != 200:
                        continue
                    items = await resp.json()
                    if not isinstance(items, list) or not items:
                        continue
                    market = items[0]
                    if not _gamma_market_matches_request(market, market_id, token_id):
                        logger.debug(
                            f"Gamma market mismatch for {market_id}: "
                            f"returned conditionId={market.get('conditionId')}"
                        )
                        continue
                    tokens = _normalize_token_ids(market.get("clobTokenIds"))
                    end_date_raw = market.get("endDateIso") or market.get("endDate")
                    try:
                        end_date = (
                            datetime.fromisoformat(end_date_raw.replace("Z", "+00:00"))
                            if end_date_raw
                            else None
                        )
                    except Exception:
                        end_date = None
                    question = (
                        market.get("question") or market.get("title") or f"Market {market_id[:30]}…"
                    )
                    return {
                        "question": question,
                        "category": market.get("category") or "unknown",
                        "token_yes": tokens[0] if len(tokens) > 0 else None,
                        "token_no": tokens[1] if len(tokens) > 1 else None,
                        "end_date": end_date,
                        "volume_24h": float(market.get("volume24hr") or 0.0),
                        "liquidity_score": float(market.get("liquidity") or 0.0),
                        "fee_rate_pct": float(
                            market.get("makerBaseFee")
                            or market.get("baseFee")
                            or market.get("fee")
                            or 0.0
                        ),
                    }
        return None
