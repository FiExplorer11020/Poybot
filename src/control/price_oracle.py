"""PriceOracle — single source of truth for the close-time `exit_price`.

Pillar 1 of the 2026-05-17 paper-trader hardening session. Replaces the
``_exit_bid`` call site (which silently fell back to ``entry_price`` on
stale cache — the May 15 phantom-win pattern) with an explicit cascade:

  1. fresh_book              — Redis ``book:last:*`` mid (≤30s, spread ≤30%)
  2. gamma last_trade_price  — Gamma /markets, cached ``gamma_cache_ttl_s``
                               s; only when the trade is ≤5 min old
  3. markets.resolved_outcome — DB lookup, returns 1.0/0.0
  4. FAIL                    — explicit; caller MUST defer the close.
                               NEVER fall back to ``entry_price`` here.

Returns an immutable ``PriceQuote`` with raw_* snapshots that feed
Pillar 5's ``close_audit_log``. Owns its own aiohttp session
(lazy-created, ``aclose()`` on shutdown).
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Literal

import aiohttp
from loguru import logger

from src.config import settings
from src.database.connection import get_db


GAMMA_BASE_URL = "https://gamma-api.polymarket.com"
GAMMA_TIMEOUT_S = 5.0
# A trade older than this is "stale" from the Gamma point-of-view: we
# may have a last_trade_price but it predates the close moment by too
# much to be a faithful exit value. Keep this conservative — Gamma
# results are tape data, not order-book quotes.
GAMMA_MAX_TRADE_AGE_S = 300.0  # 5 minutes

OracleSource = Literal["book", "gamma", "resolved", "fail"]


@dataclass(frozen=True)
class PriceQuote:
    """Immutable container for a close-time price + its provenance.

    All consumers in paper_trader treat ``price is None`` as "DEFER the
    close" — never as "use the entry price". The raw_* fields feed the
    Pillar 5 ``close_audit_log`` rows so we can replay any close to
    verify the oracle decision.
    """

    price: float | None
    source: OracleSource
    observed_ts: float  # epoch seconds — when this price was true
    spread_pct: float | None = None  # populated only for source=="book"
    raw_book: dict | None = None  # bid/ask/spread/observed_ts/source
    raw_gamma: dict | None = None  # last_trade_price/condition_id/age_s
    raw_resolution: dict | None = None  # resolved_outcome/end_date


class PriceOracle:
    """Resolve the close-time exit price for a paper trade.

    Single public entry point: ``get_close_price``. The three private
    ``_try_*`` helpers can be patched individually in tests.
    """

    def __init__(
        self,
        redis_client,
        db_pool=None,  # accepted for API symmetry; we go through get_db()
        http_session: aiohttp.ClientSession | None = None,
        *,
        gamma_cache_ttl_s: float = 60.0,
    ) -> None:
        self._redis = redis_client
        self._db_pool = db_pool  # unused — get_db() handles pool lookup
        self._http_session = http_session
        self._http_session_owned = http_session is None
        self._gamma_cache_ttl_s = float(gamma_cache_ttl_s)
        # condition_id → (fetched_at_epoch_s, payload_dict)
        self._gamma_cache: dict[str, tuple[float, dict]] = {}
        self._gamma_lock = asyncio.Lock()

    # ------------------------------------------------------------------ #
    # Public API                                                          #
    # ------------------------------------------------------------------ #

    async def get_close_price(
        self,
        market_id: str,
        token_id: str,
        direction: Literal["yes", "no"],
        *,
        prefer_resolved: bool = False,
    ) -> PriceQuote:
        """Return the canonical close price for a paper trade.

        ``prefer_resolved=True`` skips the book + Gamma steps and goes
        straight to the resolution lookup. Used by the 30d timeout path
        in paper_trader — a 30-day-old book quote is meaningless even
        if it would happen to be fresh by ``MAX_BOOK_AGE_PAPER_S``.
        """
        if not prefer_resolved:
            # Step 1: fresh order book
            book_quote = await self._try_fresh_book(market_id, token_id)
            if book_quote is not None:
                return book_quote

            # Step 2: Gamma last_trade_price
            gamma_quote = await self._try_gamma_last_trade(
                market_id, token_id, direction
            )
            if gamma_quote is not None:
                return gamma_quote

        # Step 3: markets.resolved_outcome
        resolved_quote = await self._try_resolved_outcome(
            market_id, token_id, direction
        )
        if resolved_quote is not None:
            return resolved_quote

        # Step 4: explicit failure — never fall back to entry_price
        return PriceQuote(
            price=None,
            source="fail",
            observed_ts=time.time(),
        )

    async def aclose(self) -> None:
        """Release the owned aiohttp session (no-op if injected)."""
        if self._http_session_owned and self._http_session is not None:
            try:
                await self._http_session.close()
            except Exception:
                pass
            self._http_session = None

    # ------------------------------------------------------------------ #
    # Step 1: fresh book                                                  #
    # ------------------------------------------------------------------ #

    async def _try_fresh_book(
        self,
        market_id: str,
        token_id: str,
    ) -> PriceQuote | None:
        """Read ``book:last:{market}:{token}`` and gate by age + spread.

        Returns ``None`` on cache miss, parse error, stale entry, or
        excessive spread — the next step in the cascade picks up.
        """
        if self._redis is None:
            return None
        try:
            raw = await self._redis.get(f"book:last:{market_id}:{token_id}")
        except Exception:
            return None
        if not raw:
            return None
        try:
            payload = json.loads(raw) if isinstance(raw, str) else dict(raw)
        except Exception:
            return None

        # Resolve observed timestamp (writer schema differs across
        # observer/maintenance — accept either field name).
        max_age_s = float(getattr(settings, "MAX_BOOK_AGE_PAPER_S", 60.0))
        # Pillar 1 spec says ≤30s for the oracle (stricter than the
        # default 60s). Use the tighter bound.
        max_age_s = min(max_age_s, 30.0)
        observed_raw = payload.get("observed_ts") or payload.get("captured_at")
        if observed_raw is None:
            return None
        observed_ts = _parse_epoch(observed_raw)
        if observed_ts is None:
            return None
        age_s = max(0.0, time.time() - observed_ts)
        if age_s > max_age_s:
            return None

        # Bid/ask sanity.
        try:
            best_bid = float(payload.get("best_bid"))
            best_ask = float(payload.get("best_ask"))
        except (TypeError, ValueError):
            return None
        if best_bid <= 0 or best_ask <= 0 or best_bid >= best_ask:
            return None

        # Spread gate — reuse the QW4 constant.
        max_spread_pct = float(getattr(settings, "MAX_BOOK_SPREAD_PCT", 0.30))
        mid = (best_bid + best_ask) / 2.0
        spread_pct = (best_ask - best_bid) / max(mid, 1e-6)
        if spread_pct > max_spread_pct:
            return None

        # The PriceQuote.price field is the mid — same as the legacy
        # ``_mark_mid`` semantics. We keep both bid and ask in raw_book
        # for the audit log so an analyst can reproduce the realised
        # bid value if needed.
        raw_book = {
            "best_bid": best_bid,
            "best_ask": best_ask,
            "mid": mid,
            "spread_pct": spread_pct,
            "observed_ts": observed_ts,
            "source": payload.get("source"),
            "age_s": age_s,
        }
        return PriceQuote(
            price=mid,
            source="book",
            observed_ts=observed_ts,
            spread_pct=spread_pct,
            raw_book=raw_book,
        )

    # ------------------------------------------------------------------ #
    # Step 2: Gamma last_trade_price                                      #
    # ------------------------------------------------------------------ #

    async def _try_gamma_last_trade(
        self,
        market_id: str,
        token_id: str,
        direction: Literal["yes", "no"],
    ) -> PriceQuote | None:
        """Fetch Gamma /markets and use ``last_trade_price``.

        Gamma's ``last_trade_price`` is the most recent on-chain trade
        price. We only trust it if the trade is recent
        (``last_trade_time`` within ``GAMMA_MAX_TRADE_AGE_S``). The
        result is cached for ``self._gamma_cache_ttl_s`` so repeated
        monitor ticks within the same TTL window share a single HTTP
        round-trip.
        """
        payload = await self._fetch_gamma_market(market_id)
        if payload is None:
            return None

        # Gamma returns outcomePrices/last_trade as YES/NO strings.
        # We must map back to the held token.
        last_price = _gamma_last_trade_price(payload, token_id, direction)
        if last_price is None:
            return None

        last_trade_age_s = _gamma_last_trade_age_s(payload)
        if last_trade_age_s is None or last_trade_age_s > GAMMA_MAX_TRADE_AGE_S:
            # Trade too old (or unknown age) — defer to step 3.
            return None

        observed_ts = time.time() - last_trade_age_s
        raw_gamma = {
            "last_trade_price": last_price,
            "last_trade_age_s": last_trade_age_s,
            "condition_id": payload.get("conditionId") or payload.get("condition_id"),
            "token_id": token_id,
            "direction": direction,
        }
        return PriceQuote(
            price=last_price,
            source="gamma",
            observed_ts=observed_ts,
            raw_gamma=raw_gamma,
        )

    async def _fetch_gamma_market(self, market_id: str) -> dict | None:
        """Return the cached or freshly-fetched Gamma market payload.

        Cache key is ``market_id``. ``self._gamma_lock`` serialises
        concurrent fetches to avoid duplicate HTTP traffic across
        parallel monitor ticks.
        """
        now = time.time()
        cached = self._gamma_cache.get(market_id)
        if cached is not None and (now - cached[0]) <= self._gamma_cache_ttl_s:
            return cached[1]

        async with self._gamma_lock:
            # Re-check inside the lock — another coroutine may have
            # populated the cache while we were waiting.
            cached = self._gamma_cache.get(market_id)
            if cached is not None and (now - cached[0]) <= self._gamma_cache_ttl_s:
                return cached[1]

            session = await self._ensure_session()
            url = f"{GAMMA_BASE_URL}/markets?condition_ids={market_id}"
            try:
                async with session.get(
                    url, timeout=aiohttp.ClientTimeout(total=GAMMA_TIMEOUT_S)
                ) as resp:
                    if resp.status != 200:
                        logger.debug(
                            f"PriceOracle: gamma status={resp.status} for "
                            f"market_id={market_id!r}"
                        )
                        return None
                    data = await resp.json()
            except Exception as exc:
                logger.debug(
                    f"PriceOracle: gamma fetch failed for {market_id!r}: {exc}"
                )
                return None

            # Gamma returns either a list (top-level) or a {"data": [...]}
            # wrapper depending on endpoint version. Normalise.
            if isinstance(data, dict) and "data" in data:
                rows = data["data"]
            elif isinstance(data, list):
                rows = data
            else:
                rows = []
            if not rows:
                return None
            payload = rows[0] if isinstance(rows, list) else rows
            if not isinstance(payload, dict):
                return None
            self._gamma_cache[market_id] = (time.time(), payload)
            return payload

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._http_session is None or self._http_session.closed:
            self._http_session = aiohttp.ClientSession()
            self._http_session_owned = True
        return self._http_session

    # ------------------------------------------------------------------ #
    # Step 3: markets.resolved_outcome                                    #
    # ------------------------------------------------------------------ #

    async def _try_resolved_outcome(
        self,
        market_id: str,
        token_id: str,
        direction: Literal["yes", "no"],
    ) -> PriceQuote | None:
        """Look up ``markets.resolved_outcome`` and return 1.0/0.0.

        The held token wins → 1.0; the held token loses → 0.0. Returns
        None when the column is NULL (still pending) or the market is
        unknown to us.
        """
        try:
            async with get_db() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT token_yes, token_no, resolved_outcome, end_date
                    FROM markets WHERE market_id=$1
                    """,
                    market_id,
                )
        except Exception as exc:
            logger.debug(
                f"PriceOracle: resolved_outcome lookup failed for "
                f"{market_id!r}: {exc}"
            )
            return None
        if row is None:
            return None
        outcome = row["resolved_outcome"]
        if outcome is None:
            return None
        outcome_str = str(outcome).strip().lower()
        if outcome_str in ("yes", "1", "true"):
            winning_token = row["token_yes"]
        elif outcome_str in ("no", "0", "false"):
            winning_token = row["token_no"]
        else:
            return None

        price = 1.0 if token_id == winning_token else 0.0
        end_date = row["end_date"]
        raw_resolution = {
            "resolved_outcome": outcome_str,
            "winning_token": winning_token,
            "held_token": token_id,
            "direction": direction,
            "end_date": end_date.isoformat() if end_date is not None else None,
        }
        return PriceQuote(
            price=price,
            source="resolved",
            observed_ts=time.time(),
            raw_resolution=raw_resolution,
        )


# ---------------------------------------------------------------------- #
# Pure helpers — kept module-level so tests can exercise them directly.  #
# ---------------------------------------------------------------------- #


def _parse_epoch(value) -> float | None:
    """Best-effort epoch-seconds parser (handles float, int, ISO string)."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        from datetime import datetime, timezone as _tz

        obs_str = str(value)
        if obs_str.endswith("Z"):
            obs_str = obs_str[:-1] + "+00:00"
        dt = datetime.fromisoformat(obs_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_tz.utc)
        return dt.timestamp()
    except (ValueError, TypeError):
        return None


def _gamma_last_trade_price(
    payload: dict,
    token_id: str,
    direction: Literal["yes", "no"],
) -> float | None:
    """Extract last_trade_price from a Gamma market payload, mapped to
    the held token.

    Gamma payloads can carry the price in several shapes:
      * ``last_trade_price`` as a top-level float
      * ``lastTradePrice`` (camelCase variant on some endpoints)
      * ``outcomePrices`` (JSON-encoded list ``"[0.62, 0.38]"`` with
        index 0 = YES, index 1 = NO — used by /markets)

    We try them in order and return None if none parse.
    """
    # Variant A: scalar
    for key in ("last_trade_price", "lastTradePrice"):
        val = payload.get(key)
        if val is not None:
            try:
                p = float(val)
                # Scalar form is the YES price by convention. Map to
                # the held token.
                token_yes = payload.get("token_yes") or payload.get("tokenYes")
                if token_yes is None:
                    # Conservative: assume direction is the source of truth.
                    return p if direction == "yes" else 1.0 - p
                return p if token_id == token_yes else 1.0 - p
            except (TypeError, ValueError):
                pass

    # Variant B: outcomePrices list
    raw = payload.get("outcomePrices") or payload.get("outcome_prices")
    if raw is None:
        return None
    try:
        prices = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(prices, list) or len(prices) < 2:
            return None
        yes_price = float(prices[0])
        no_price = float(prices[1])
    except (TypeError, ValueError, json.JSONDecodeError):
        return None

    # Disambiguate via token mapping when available.
    token_yes = payload.get("token_yes") or payload.get("tokenYes")
    if token_yes is not None:
        return yes_price if token_id == token_yes else no_price
    # Fall back to direction.
    return yes_price if direction == "yes" else no_price


def _gamma_last_trade_age_s(payload: dict) -> float | None:
    """Return seconds since Gamma's last_trade_time, or None if unknown."""
    for key in ("last_trade_time", "lastTradeTime"):
        val = payload.get(key)
        if val is None:
            continue
        ts = _parse_epoch(val)
        if ts is None:
            continue
        return max(0.0, time.time() - ts)
    return None
