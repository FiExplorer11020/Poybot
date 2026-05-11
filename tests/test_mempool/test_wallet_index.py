"""Smoke test for src.mempool.wallet_index — Wave-2 replaces with real tests."""
import pytest


def test_module_imports():
    """Confirms the module imports without errors. Wave-2 agents will
    replace this with real tests for WatchedWalletIndex bloom semantics
    (add / __contains__ / refresh_from_universe / run_refresh_loop)."""
    from src.mempool import wallet_index  # noqa: F401
