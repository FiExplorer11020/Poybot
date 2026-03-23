import asyncio

import pytest

from app.services.order_signer import PolymarketOrderSigner
from app.services.trade_executor import ExecutionRequest


def _request() -> ExecutionRequest:
    return ExecutionRequest(
        market_id="m1",
        market_title="Market",
        token_id="123",
        side="BUY_YES",
        price=0.42,
        size=10.5,
        notional=4.41,
        risk_pct=0.2,
        expected_edge=0.03,
    )


def test_order_signer_places_limit_order_with_mocked_py_clob_client(monkeypatch) -> None:
    from app.services import order_signer as order_signer_module

    calls: dict[str, object] = {}

    class FakePyClobClient:
        def __init__(self, **kwargs) -> None:
            calls["init"] = kwargs

        def set_api_creds(self, creds) -> None:
            calls["creds"] = creds

        def create_and_post_order(self, order_args):
            calls["order_args"] = order_args
            return {
                "orderID": "order-1",
                "status": "SUBMITTED",
                "transactionsHashes": ["0xtx"],
            }

    monkeypatch.setattr(order_signer_module, "PyClobClient", FakePyClobClient)

    signer = PolymarketOrderSigner(
        private_key="0xabc",
        api_key="api-key",
        api_secret="api-secret",
        api_passphrase="api-passphrase",
    )
    out = asyncio.run(signer.place_limit_order(_request()))

    assert out == {
        "order_id": "order-1",
        "status": "SUBMITTED",
        "tx_hash": "0xtx",
        "raw": {
            "orderID": "order-1",
            "status": "SUBMITTED",
            "transactionsHashes": ["0xtx"],
        },
    }
    assert calls["init"] == {
        "host": "https://clob.polymarket.com",
        "key": "0xabc",
        "chain_id": 137,
    }
    assert calls["creds"].api_key == "api-key"
    assert calls["order_args"].side == "BUY"


def test_order_signer_retries_once_on_timeout(monkeypatch) -> None:
    from app.services import order_signer as order_signer_module

    class FakePyClobClient:
        def __init__(self, **kwargs) -> None:
            self.calls = 0

        def set_api_creds(self, creds) -> None:
            self.creds = creds

        def create_and_post_order(self, order_args):
            self.calls += 1
            if self.calls == 1:
                raise TimeoutError("first timeout")
            return {"orderID": "order-2", "status": "SUBMITTED", "transactionsHashes": []}

    monkeypatch.setattr(order_signer_module, "PyClobClient", FakePyClobClient)

    signer = PolymarketOrderSigner(
        private_key="0xabc",
        api_key="api-key",
        api_secret="api-secret",
        api_passphrase="api-passphrase",
    )
    out = asyncio.run(signer.place_limit_order(_request()))

    assert out["order_id"] == "order-2"
    assert signer._client.calls == 2


def test_order_signer_raises_after_second_timeout(monkeypatch) -> None:
    from app.services import order_signer as order_signer_module

    class FakePyClobClient:
        def __init__(self, **kwargs) -> None:
            self.calls = 0

        def set_api_creds(self, creds) -> None:
            self.creds = creds

        def create_and_post_order(self, order_args):
            self.calls += 1
            raise TimeoutError("still timing out")

    monkeypatch.setattr(order_signer_module, "PyClobClient", FakePyClobClient)

    signer = PolymarketOrderSigner(
        private_key="0xabc",
        api_key="api-key",
        api_secret="api-secret",
        api_passphrase="api-passphrase",
    )

    with pytest.raises(TimeoutError):
        asyncio.run(signer.place_limit_order(_request()))

    assert signer._client.calls == 2


def test_order_signer_cancel_all_returns_cancelled_count(monkeypatch) -> None:
    from app.services import order_signer as order_signer_module

    class FakePyClobClient:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs

        def set_api_creds(self, creds) -> None:
            self.creds = creds

        def cancel_all(self):
            return {"canceled": ["a", "b", "c"]}

    monkeypatch.setattr(order_signer_module, "PyClobClient", FakePyClobClient)

    signer = PolymarketOrderSigner(
        private_key="0xabc",
        api_key="api-key",
        api_secret="api-secret",
        api_passphrase="api-passphrase",
    )
    out = asyncio.run(signer.cancel_all())

    assert out["cancelled"] == 3
