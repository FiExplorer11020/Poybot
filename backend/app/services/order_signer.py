from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any, TypeVar

import httpx

try:
    from py_clob_client.client import ClobClient as PyClobClient
    from py_clob_client.clob_types import ApiCreds, OrderArgs
except ModuleNotFoundError:  # pragma: no cover - exercised indirectly in tests with monkeypatching
    class _MissingPyClobClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            raise ModuleNotFoundError(
                "py-clob-client is required for live Polymarket order signing"
            )

    @dataclass
    class ApiCreds:
        api_key: str
        api_secret: str
        api_passphrase: str

    @dataclass
    class OrderArgs:
        token_id: str
        price: float
        size: float
        side: str

    PyClobClient = _MissingPyClobClient

if TYPE_CHECKING:
    from app.services.trade_executor import ExecutionRequest

log = logging.getLogger(__name__)

T = TypeVar("T")


class OrderSide(StrEnum):
    BUY = "BUY"


class PolymarketOrderSigner:
    def __init__(
        self,
        private_key: str,
        chain_id: int = 137,
        host: str = "https://clob.polymarket.com",
        api_key: str | None = None,
        api_secret: str | None = None,
        api_passphrase: str | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        self._client = PyClobClient(
            host=host,
            key=private_key,
            chain_id=chain_id,
        )
        self._timeout_seconds = timeout_seconds
        self._api_key = api_key
        self._api_secret = api_secret
        self._api_passphrase = api_passphrase
        self._api_creds_configured = False
        self._configure_api_creds_if_available()

    async def place_limit_order(self, req: ExecutionRequest) -> dict[str, Any]:
        await self._ensure_api_creds()
        order_args = OrderArgs(
            token_id=req.token_id,
            price=req.price,
            size=req.size,
            side=OrderSide.BUY.value,
        )
        resp = await self._run_with_timeout_retry(
            self._client.create_and_post_order,
            order_args,
            operation_name="create_and_post_order",
        )
        tx_hashes = resp.get("transactionsHashes") or []
        tx_hash = tx_hashes[0] if isinstance(tx_hashes, list) and tx_hashes else None
        return {
            "order_id": resp.get("orderID"),
            "status": resp.get("status"),
            "tx_hash": tx_hash,
            "raw": resp,
        }

    async def cancel_all(self) -> dict[str, Any]:
        await self._ensure_api_creds()
        resp = await self._run_with_timeout_retry(
            self._client.cancel_all,
            operation_name="cancel_all",
        )
        canceled = resp.get("canceled") or resp.get("cancelled") or []
        cancelled_count = len(canceled) if isinstance(canceled, list) else int(bool(canceled))
        return {"cancelled": cancelled_count, "raw": resp}

    def _configure_api_creds_if_available(self) -> None:
        if not (
            self._api_key
            and self._api_secret
            and self._api_passphrase
            and hasattr(self._client, "set_api_creds")
        ):
            return

        self._client.set_api_creds(
            ApiCreds(
                api_key=self._api_key,
                api_secret=self._api_secret,
                api_passphrase=self._api_passphrase,
            )
        )
        self._api_creds_configured = True

    async def _ensure_api_creds(self) -> None:
        if self._api_creds_configured:
            return
        if not hasattr(self._client, "create_or_derive_api_creds"):
            return

        creds = await self._run_with_timeout_retry(
            self._client.create_or_derive_api_creds,
            operation_name="create_or_derive_api_creds",
        )
        if creds is None:
            raise RuntimeError("unable to derive Polymarket API credentials")
        if hasattr(self._client, "set_api_creds"):
            self._client.set_api_creds(creds)
        self._api_creds_configured = True

    async def _run_with_timeout_retry(
        self,
        func: Callable[..., T],
        *args: Any,
        operation_name: str,
    ) -> T:
        for attempt in range(2):
            try:
                return await asyncio.wait_for(
                    asyncio.to_thread(func, *args),
                    timeout=self._timeout_seconds,
                )
            except Exception as exc:
                if not self._is_timeout_error(exc):
                    raise
                if attempt == 1:
                    raise
                log.warning("Polymarket %s timed out, retrying once", operation_name)

        raise RuntimeError(f"unreachable timeout retry state for {operation_name}")

    @staticmethod
    def _is_timeout_error(exc: Exception) -> bool:
        if isinstance(exc, (asyncio.TimeoutError, TimeoutError, httpx.TimeoutException)):
            return True
        return exc.__class__.__name__.lower().endswith("timeout")
