import asyncio

import pytest

from app.core import settings as settings_module
from app.services.trade_executor import ExecutionRequest, TradeExecutor


@pytest.fixture(autouse=True)
def reset_settings_cache():
    settings_module.get_settings.cache_clear()
    yield
    settings_module.get_settings.cache_clear()


def _live_request() -> ExecutionRequest:
    return ExecutionRequest(
        market_id="m1",
        market_title="T",
        token_id="tok",
        side="BUY_YES",
        price=0.55,
        size=100,
        notional=55,
        risk_pct=0.5,
        expected_edge=0.02,
    )


def test_trade_executor_dry_run_mode() -> None:
    executor = TradeExecutor()
    out = asyncio.run(executor.execute(_live_request()))
    assert out["execution_mode"] == "dry_run"
    assert out["exchange_status"] == "SIMULATED"


def test_trade_executor_live_mode_uses_signed_order_flow(monkeypatch) -> None:
    from app.services import order_signer as order_signer_module

    cfg = settings_module.get_settings()
    cfg.polymarket_trading_enabled = True
    cfg.polymarket_trading_mode = "live"
    cfg.polymarket_private_key = "0xabc"
    cfg.polymarket_api_key = "api-key"
    cfg.polymarket_api_secret = "api-secret"
    cfg.polymarket_api_passphrase = "api-passphrase"

    calls: dict[str, object] = {}

    class FakePyClobClient:
        def __init__(self, **kwargs) -> None:
            calls["init"] = kwargs

        def set_api_creds(self, creds) -> None:
            calls["creds"] = creds

        def create_and_post_order(self, order_args):
            calls["order_args"] = order_args
            return {
                "orderID": "ord-123",
                "status": "MATCHED",
                "transactionsHashes": ["0xhash"],
            }

    monkeypatch.setattr(order_signer_module, "PyClobClient", FakePyClobClient)

    executor = TradeExecutor()
    out = asyncio.run(executor.execute(_live_request()))

    assert out["execution_mode"] == "live"
    assert out["order_id"] == "ord-123"
    assert out["exchange_status"] == "MATCHED"
    assert out["tx_hash"] == "0xhash"
    assert calls["init"] == {
        "host": cfg.polymarket_clob_rest_base_url,
        "key": "0xabc",
        "chain_id": 137,
    }
    assert calls["creds"].api_key == "api-key"
    assert calls["order_args"].token_id == "tok"
    assert calls["order_args"].side == "BUY"


def test_trade_executor_live_order_book_error_returns_rejected(monkeypatch, caplog) -> None:
    from app.services import order_signer as order_signer_module

    cfg = settings_module.get_settings()
    cfg.polymarket_trading_enabled = True
    cfg.polymarket_trading_mode = "live"
    cfg.polymarket_private_key = "0xabc"
    cfg.polymarket_api_key = "api-key"
    cfg.polymarket_api_secret = "api-secret"
    cfg.polymarket_api_passphrase = "api-passphrase"

    class OrderBookError(Exception):
        pass

    class FakePyClobClient:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs

        def set_api_creds(self, creds) -> None:
            self.creds = creds

        def create_and_post_order(self, order_args):
            raise OrderBookError("book unavailable")

    monkeypatch.setattr(order_signer_module, "PyClobClient", FakePyClobClient)

    executor = TradeExecutor()
    out = asyncio.run(executor.execute(_live_request()))

    assert out["execution_mode"] == "live"
    assert out["status"] == "REJECTED"
    assert out["exchange_status"] == "REJECTED"
    assert out["raw"]["error_type"] == "OrderBookError"
    assert "0xabc" not in caplog.text


def test_trade_executor_live_insufficient_funds_raises(monkeypatch) -> None:
    from app.services import order_signer as order_signer_module
    from app.services.trade_executor import InsufficientFundsException

    cfg = settings_module.get_settings()
    cfg.polymarket_trading_enabled = True
    cfg.polymarket_trading_mode = "live"
    cfg.polymarket_private_key = "0xabc"
    cfg.polymarket_api_key = "api-key"
    cfg.polymarket_api_secret = "api-secret"
    cfg.polymarket_api_passphrase = "api-passphrase"

    class InsufficientFundsError(Exception):
        pass

    class FakePyClobClient:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs

        def set_api_creds(self, creds) -> None:
            self.creds = creds

        def create_and_post_order(self, order_args):
            raise InsufficientFundsError("not enough balance")

    monkeypatch.setattr(order_signer_module, "PyClobClient", FakePyClobClient)

    executor = TradeExecutor()
    with pytest.raises(InsufficientFundsException):
        asyncio.run(executor.execute(_live_request()))
