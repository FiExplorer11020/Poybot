"""Smoke test for src.mempool.tx_decoder — Wave-2 replaces with real tests."""
import pytest


def test_module_imports():
    """Confirms the module imports without errors. Wave-2 agents will
    replace this with real tests covering CLOBTxDecoder.decode() against
    synthetic fillOrder / matchOrders / cancelOrder calldata."""
    from src.mempool import tx_decoder  # noqa: F401
