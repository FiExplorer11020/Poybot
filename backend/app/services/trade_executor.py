from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from app.clients.clob import ClobClient
from app.core.settings import get_settings


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


class TradeExecutor:
    """Polymarket execution bridge with dry-run + configurable real mode."""

    def __init__(self, clob_client: ClobClient | None = None) -> None:
        self.settings = get_settings()
        self._owns_client = clob_client is None
        self.clob = clob_client or ClobClient(self.settings.polymarket_clob_rest_base_url)

    async def execute(self, req: ExecutionRequest) -> dict[str, Any]:
        mode = self.settings.polymarket_trading_mode.lower().strip()
        if not self.settings.polymarket_trading_enabled or mode != "live":
            return {
                "execution_mode": "dry_run",
                "order_id": f"dry-{int(datetime.now(timezone.utc).timestamp() * 1000)}",
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

        payload = {
            "market": req.market_id,
            "token_id": req.token_id,
            "side": req.side,
            "price": str(Decimal(str(req.price))),
            "size": str(Decimal(str(req.size))),
            "order_type": "GTC",
        }
        headers = {
            "POLY_API_KEY": self.settings.polymarket_api_key or "",
            "POLY_API_SECRET": self.settings.polymarket_api_secret or "",
            "POLY_PASSPHRASE": self.settings.polymarket_api_passphrase or "",
        }
        resp = await self.clob.place_order(payload, headers=headers, endpoint=self.settings.polymarket_order_endpoint)
        return {
            "execution_mode": "live",
            "order_id": str(resp.get("orderID") or resp.get("order_id") or resp.get("id") or "unknown"),
            "exchange_status": str(resp.get("status") or "SUBMITTED"),
            "tx_hash": resp.get("txHash") or resp.get("tx_hash"),
            "raw": resp,
        }

    async def close(self) -> None:
        if self._owns_client:
            await self.clob.close()
