from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from app.clients.clob import ClobClient
from app.core.settings import get_settings
from app.services.order_signer import PolymarketOrderSigner

log = logging.getLogger(__name__)


@dataclass
class ExecutionRequest:
    market_id: str
    market_title: str
    token_id: str
    side: str
    price: float
    size: float
    notional: float
    risk_pct: float
    expected_edge: float


class InsufficientFundsException(RuntimeError):  # noqa: N818
    """Raised when the live exchange account cannot fund a new order."""


class TradeExecutor:
    """Polymarket execution bridge with dry-run + configurable real mode."""

    def __init__(
        self,
        clob_client: ClobClient | None = None,
        order_signer: PolymarketOrderSigner | None = None,
    ) -> None:
        self.settings = get_settings()
        self._owns_client = clob_client is None
        self.clob = clob_client or ClobClient(self.settings.polymarket_clob_rest_base_url)
        self._order_signer = order_signer

    async def execute(self, req: ExecutionRequest) -> dict[str, Any]:
        mode = self.settings.polymarket_trading_mode.lower().strip()
        if not self.settings.polymarket_trading_enabled or mode != "live":
            return {
                "execution_mode": "dry_run",
                "order_id": f"dry-{int(datetime.now(UTC).timestamp() * 1000)}",
                "exchange_status": "SIMULATED",
                "tx_hash": None,
                "raw": {
                    "market": req.market_id,
                    "token_id": req.token_id,
                    "side": req.side,
                    "price": req.price,
                    "size": req.size,
                    "notional": req.notional,
                },
            }

        try:
            resp = await self._get_order_signer().place_limit_order(req)
        except Exception as exc:
            if self._exception_matches(exc, "OrderBookError"):
                log.warning(
                    (
                        "Polymarket order rejected by order book "
                        "for market=%s token_id=%s side=%s price=%s size=%s: %s"
                    ),
                    req.market_id,
                    req.token_id,
                    req.side,
                    Decimal(str(req.price)),
                    Decimal(str(req.size)),
                    exc,
                )
                raw = {
                    "error_type": exc.__class__.__name__,
                    "message": str(exc),
                    "market": req.market_id,
                    "token_id": req.token_id,
                    "side": req.side,
                }
                return {
                    "execution_mode": "live",
                    "order_id": "rejected",
                    "status": "REJECTED",
                    "exchange_status": "REJECTED",
                    "tx_hash": None,
                    "raw": raw,
                }
            if self._exception_matches(exc, "InsufficientFundsError"):
                raise InsufficientFundsException(
                    "insufficient funds for Polymarket live order"
                ) from exc
            raise

        exchange_status = str(resp.get("status") or "SUBMITTED")
        return {
            "execution_mode": "live",
            "order_id": str(
                resp.get("orderID") or resp.get("order_id") or resp.get("id") or "unknown"
            ),
            "status": exchange_status,
            "exchange_status": exchange_status,
            "tx_hash": resp.get("txHash") or resp.get("tx_hash"),
            "raw": resp.get("raw") if isinstance(resp.get("raw"), dict) else resp,
        }

    async def close(self) -> None:
        if self._owns_client:
            await self.clob.close()

    def _get_order_signer(self) -> PolymarketOrderSigner:
        if self._order_signer is None:
            private_key = (self.settings.polymarket_private_key or "").strip()
            if not private_key:
                raise ValueError("polymarket_private_key is required for live trading")
            self._order_signer = PolymarketOrderSigner(
                private_key=private_key,
                host=self.settings.polymarket_clob_rest_base_url,
                api_key=self.settings.polymarket_api_key,
                api_secret=self.settings.polymarket_api_secret,
                api_passphrase=self.settings.polymarket_api_passphrase,
            )
        return self._order_signer

    @staticmethod
    def _exception_matches(exc: Exception, expected_name: str) -> bool:
        current: BaseException | None = exc
        while current is not None:
            if current.__class__.__name__ == expected_name:
                return True
            current = current.__cause__ or current.__context__
        return False
