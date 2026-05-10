"""
Async wrapper around py-clob-client for the LiveTrader (S2.6).

Why a wrapper?
  * py-clob-client (the official Polymarket SDK) is synchronous and
    blocks on every HTTP call. Running it directly inside the asyncio
    event loop would freeze observer/engine for tens of ms per call.
  * We isolate every CLOB call in `loop.run_in_executor(...)` so the
    blocking I/O happens on a thread, while the rest of the engine
    keeps moving.
  * Centralizing all CLOB access here means there's exactly one place
    that knows about the SDK's quirks (synchronous returns, retry
    semantics, magic-wallet vs proxy-wallet signature flow). Tests
    monkey-patch this module, never the SDK directly.

This wrapper is dry-run aware: when `dry_run=True` (or no private key
is configured), every "write" call (place_order, cancel_order) is
short-circuited with a dummy response and a structured log entry. Read
calls (get_orderbook, get_midpoint, get_trades_history) still hit the
real CLOB even in dry-run, because we want price discovery to work.

Public surface (everything `async`):
    - get_midpoint(token_id) -> float
    - get_orderbook(token_id) -> OrderbookSnapshot
    - place_limit_order(...)  -> PlaceOrderResult
    - cancel_order(order_id)  -> bool
    - get_order_status(order_id) -> OrderStatus | None
    - get_trades_for_order(order_id) -> list[FillEvent]
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Optional

from loguru import logger

from src.config import settings


# --------------------------------------------------------------------------- #
# Public dataclasses                                                          #
# --------------------------------------------------------------------------- #


@dataclass
class OrderbookSnapshot:
    """Top-of-book + best bid/ask for sizing decisions.

    `mid` is computed by the wrapper rather than read from the CLOB
    response, so a flat `mid` semantic ((bid+ask)/2) is enforced
    regardless of any future SDK change.
    """
    token_id: str
    best_bid: float
    best_ask: float
    bid_size: float
    ask_size: float
    mid: float


@dataclass
class PlaceOrderResult:
    """Outcome of a place_order call.

    `success=True` means the order was accepted by the CLOB and is on
    the book OR shadow-logged (when dry_run). `clob_order_id` is None
    for shadow rows.
    """
    success: bool
    clob_order_id: Optional[str]
    error_message: Optional[str] = None
    raw_response: dict[str, Any] = field(default_factory=dict)
    shadow: bool = False


@dataclass
class OrderStatus:
    """Snapshot of an order's lifecycle on the CLOB.

    `state` matches our `live_orders.order_state` vocabulary so callers
    can use it directly without translating.
    """
    clob_order_id: str
    state: str  # 'placed' / 'filled' / 'partial' / 'canceled' / 'rejected' / 'expired'
    filled_size: float
    remaining_size: float
    avg_fill_price: Optional[float] = None


@dataclass
class FillEvent:
    """One fill (full or partial) for a given CLOB order."""
    clob_order_id: str
    fill_price: float
    fill_size: float
    fee_usdc: float
    occurred_at: float  # unix seconds
    raw: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Wrapper                                                                     #
# --------------------------------------------------------------------------- #


# Sentinel that callers can use to test "did we instantiate a live SDK?"
_CLIENT_NOT_INITIALIZED = object()


class CLOBClientWrapper:
    """Async wrapper around py-clob-client.

    Construction does NOT instantiate the underlying SDK — that happens
    lazily inside `_get_client()` so that import-time of this module is
    free of side effects (good for tests, good for entry points that
    might decide based on settings whether to bootstrap the wrapper at
    all).
    """

    def __init__(
        self,
        *,
        clob_url: Optional[str] = None,
        chain_id: Optional[int] = None,
        private_key: Optional[str] = None,
        funder_address: Optional[str] = None,
        dry_run: Optional[bool] = None,
    ) -> None:
        self._clob_url = clob_url or settings.POLYMARKET_CLOB_URL
        self._chain_id = chain_id if chain_id is not None else settings.POLYMARKET_CHAIN_ID
        self._private_key = private_key if private_key is not None else settings.POLYMARKET_PRIVATE_KEY
        self._funder_address = (
            funder_address if funder_address is not None else settings.POLYMARKET_FUNDER_ADDRESS
        )
        # Dry-run is true iff explicitly requested OR no key configured.
        explicit_dry_run = settings.LIVE_TRADING_DRY_RUN if dry_run is None else dry_run
        self._dry_run = bool(explicit_dry_run) or not bool(self._private_key)
        self._client: Any = _CLIENT_NOT_INITIALIZED
        self._client_lock = asyncio.Lock()

    @property
    def dry_run(self) -> bool:
        return self._dry_run

    @property
    def funder_address(self) -> str:
        return self._funder_address

    # ------------------------------------------------------------------ #
    # Lazy SDK instantiation                                              #
    # ------------------------------------------------------------------ #

    async def _get_client(self) -> Any:
        """Lazy-build the underlying ClobClient on first use.

        We grab a lock so two coroutines hitting the wrapper at the same
        moment don't race-build two SDK instances (each of which would
        do its own EIP-712 keypair derivation).
        """
        if self._client is not _CLIENT_NOT_INITIALIZED:
            return self._client
        async with self._client_lock:
            if self._client is not _CLIENT_NOT_INITIALIZED:
                return self._client
            self._client = await asyncio.get_running_loop().run_in_executor(
                None, self._build_client_sync
            )
            return self._client

    def _build_client_sync(self) -> Any:
        """Construct the SDK in a thread.

        Imported lazily so that running unit tests without the SDK
        installed (or in an environment without secrets) is fine — we
        only hit this code on the production VM.
        """
        # Local import: keeps `import src.engine.clob_client_wrapper`
        # cheap for code paths that never use it.
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds  # noqa: F401  # may be needed for L2 auth later

        client = ClobClient(
            host=self._clob_url,
            chain_id=self._chain_id,
            key=self._private_key,
            funder=self._funder_address,
            signature_type=2,  # 2 = Polymarket proxy/Magic wallet
        )
        # Derive L2 (CLOB API) credentials from the private key. These are
        # required for placing orders on the authenticated endpoints.
        try:
            api_creds = client.create_or_derive_api_creds()
            client.set_api_creds(api_creds)
        except Exception as e:
            # Surface clearly — this is the most common boot-time failure
            # ("API key already exists" or "invalid signature_type").
            logger.error(f"CLOB client API creds derivation failed: {e}")
            raise
        return client

    # ------------------------------------------------------------------ #
    # Read calls (always hit real CLOB, even in dry_run)                  #
    # ------------------------------------------------------------------ #

    async def get_midpoint(self, token_id: str) -> float:
        """Return the current mid-price for a CLOB token.

        Used by OrderManager to compute limit prices. We use the SDK's
        `get_midpoint()` which is a single CLOB call (cheaper than
        fetching the full orderbook just to compute mid).
        """
        client = await self._get_client()
        loop = asyncio.get_running_loop()
        # SDK returns {"mid": "0.532000"} as strings.
        resp = await loop.run_in_executor(None, client.get_midpoint, token_id)
        return float(resp.get("mid") or resp)

    async def get_orderbook(self, token_id: str) -> OrderbookSnapshot:
        """Return top-of-book — needed when we want best_bid/ask separately
        (e.g. to size a "cross spread" market order, or to check available
        liquidity before posting a too-large limit order)."""
        client = await self._get_client()
        loop = asyncio.get_running_loop()
        book = await loop.run_in_executor(None, client.get_order_book, token_id)
        # SDK returns OrderBookSummary with `bids` / `asks` as ordered
        # lists of {price, size}, best price last (highest bid first /
        # lowest ask first depending on side). The SDK uses an attribute,
        # but some forks return a dict — we accept both. Important: an
        # *empty* list must NOT fall through to .get(); we special-case
        # the attribute hit explicitly.
        if hasattr(book, "bids"):
            bids = book.bids
            asks = book.asks
        else:
            bids = book.get("bids", []) if isinstance(book, dict) else []
            asks = book.get("asks", []) if isinstance(book, dict) else []

        def _top(levels: list[Any], side: str) -> tuple[float, float]:
            if not levels:
                return 0.0, 0.0
            top = levels[-1] if side == "bid" else levels[0]
            price = float(getattr(top, "price", None) or top.get("price"))
            size = float(getattr(top, "size", None) or top.get("size"))
            return price, size

        best_bid, bid_size = _top(bids, "bid")
        best_ask, ask_size = _top(asks, "ask")
        mid = (best_bid + best_ask) / 2 if (best_bid > 0 and best_ask > 0) else 0.0
        return OrderbookSnapshot(
            token_id=token_id,
            best_bid=best_bid,
            best_ask=best_ask,
            bid_size=bid_size,
            ask_size=ask_size,
            mid=mid,
        )

    # ------------------------------------------------------------------ #
    # Write calls (short-circuited in dry_run)                            #
    # ------------------------------------------------------------------ #

    async def place_limit_order(
        self,
        *,
        token_id: str,
        side: str,
        price: float,
        size: float,
    ) -> PlaceOrderResult:
        """Place a GTC limit order. Returns a structured result whether
        the call was real or shadowed.

        side: 'BUY' or 'SELL' (uppercase — the SDK is case-sensitive).
        price: 0 < price < 1 (Polymarket prices are probabilities).
        size: number of shares (NOT USDC notional; caller converts).
        """
        side_upper = side.upper()
        if side_upper not in {"BUY", "SELL"}:
            return PlaceOrderResult(
                success=False,
                clob_order_id=None,
                error_message=f"invalid side {side!r}",
            )
        if not (0.0 < price < 1.0):
            return PlaceOrderResult(
                success=False,
                clob_order_id=None,
                error_message=f"price {price} out of (0,1)",
            )
        if size <= 0:
            return PlaceOrderResult(
                success=False,
                clob_order_id=None,
                error_message=f"non-positive size {size}",
            )

        if self._dry_run:
            logger.info(
                "[CLOB shadow] would place limit "
                f"{side_upper} {size:.4f} @ {price:.4f} on token {token_id[:14]}…"
            )
            return PlaceOrderResult(
                success=True,
                clob_order_id=None,
                shadow=True,
                raw_response={
                    "shadow": True,
                    "token_id": token_id,
                    "side": side_upper,
                    "price": price,
                    "size": size,
                },
            )

        try:
            client = await self._get_client()
            from py_clob_client.clob_types import OrderArgs, OrderType

            args = OrderArgs(
                token_id=token_id,
                price=price,
                size=size,
                side=side_upper,
            )
            loop = asyncio.get_running_loop()
            signed = await loop.run_in_executor(None, client.create_order, args)
            resp = await loop.run_in_executor(
                None, client.post_order, signed, OrderType.GTC
            )
        except Exception as e:
            logger.error(f"CLOB place_limit_order failed: {e}")
            return PlaceOrderResult(
                success=False,
                clob_order_id=None,
                error_message=str(e),
            )

        success = bool(resp.get("success") if isinstance(resp, dict) else False)
        order_id = resp.get("orderID") if isinstance(resp, dict) else None
        if not success:
            err = (resp.get("errorMsg") if isinstance(resp, dict) else None) or "unknown"
            return PlaceOrderResult(
                success=False,
                clob_order_id=order_id,
                error_message=err,
                raw_response=resp if isinstance(resp, dict) else {"raw": resp},
            )
        return PlaceOrderResult(
            success=True,
            clob_order_id=order_id,
            raw_response=resp if isinstance(resp, dict) else {"raw": resp},
        )

    async def cancel_order(self, clob_order_id: str) -> bool:
        """Cancel an order. Returns True iff the CLOB acknowledged it."""
        if self._dry_run:
            logger.info(f"[CLOB shadow] would cancel order {clob_order_id}")
            return True
        try:
            client = await self._get_client()
            loop = asyncio.get_running_loop()
            resp = await loop.run_in_executor(None, client.cancel, clob_order_id)
            # SDK returns {"canceled": [order_id], "not_canceled": {...}}
            canceled = resp.get("canceled") if isinstance(resp, dict) else None
            return bool(canceled and clob_order_id in canceled)
        except Exception as e:
            logger.error(f"CLOB cancel_order({clob_order_id}) failed: {e}")
            return False

    async def get_order_status(self, clob_order_id: str) -> Optional[OrderStatus]:
        """Return current status of an order, or None if unknown.

        In dry_run we don't have anything to query (no real order exists),
        so we return None and let the caller fall back to its own state.
        """
        if self._dry_run:
            return None
        try:
            client = await self._get_client()
            loop = asyncio.get_running_loop()
            data = await loop.run_in_executor(None, client.get_order, clob_order_id)
        except Exception as e:
            logger.error(f"CLOB get_order_status({clob_order_id}) failed: {e}")
            return None
        if not data:
            return None
        # Normalize SDK status -> our vocabulary.
        raw_status = (data.get("status") or "").upper()
        size_matched = float(data.get("size_matched") or 0)
        original_size = float(data.get("original_size") or data.get("size") or 0)
        remaining = max(0.0, original_size - size_matched)
        if raw_status == "MATCHED" or (size_matched > 0 and remaining == 0):
            state = "filled"
        elif size_matched > 0 and remaining > 0:
            state = "partial"
        elif raw_status == "CANCELED":
            state = "canceled"
        elif raw_status == "EXPIRED":
            state = "expired"
        elif raw_status in {"LIVE", "DELAYED", ""}:
            state = "placed"
        else:
            state = "placed"
        avg_price = data.get("price")
        return OrderStatus(
            clob_order_id=clob_order_id,
            state=state,
            filled_size=size_matched,
            remaining_size=remaining,
            avg_fill_price=float(avg_price) if avg_price is not None else None,
        )

    async def get_trades_for_order(self, clob_order_id: str) -> list[FillEvent]:
        """Return the list of fills produced by an order.

        Polymarket exposes a `/trades` endpoint that lists fills for the
        authenticated maker; we filter by order id locally because the
        SDK doesn't accept an order_id filter natively in older versions.
        """
        if self._dry_run:
            return []
        try:
            client = await self._get_client()
            loop = asyncio.get_running_loop()
            trades = await loop.run_in_executor(None, client.get_trades)
        except Exception as e:
            logger.error(f"CLOB get_trades_for_order({clob_order_id}) failed: {e}")
            return []

        events: list[FillEvent] = []
        for t in trades or []:
            order_id = t.get("order_id") or t.get("orderID")
            if order_id != clob_order_id:
                continue
            try:
                events.append(
                    FillEvent(
                        clob_order_id=clob_order_id,
                        fill_price=float(t.get("price") or 0),
                        fill_size=float(t.get("size") or 0),
                        fee_usdc=float(t.get("fee") or 0),
                        occurred_at=float(t.get("match_time") or 0),
                        raw=t,
                    )
                )
            except (TypeError, ValueError) as e:
                logger.warning(f"skipping malformed CLOB trade {t}: {e}")
        return events
