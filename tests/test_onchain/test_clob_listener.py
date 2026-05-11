"""Wave-1 smoke test for src.onchain.clob_listener.

Confirms the module imports without errors. Wave-2 agents will replace
this with real tests covering subscription reconnect semantics, batch
commit invariants vs the chain_sync_state cursor, and trades_observed
UPSERT dedup.
"""

import pytest  # noqa: F401

import src.onchain.clob_listener  # noqa: F401


def test_module_imports():
    """Smoke test — confirms the module imports without errors. Wave-2
    agents will replace this with real tests."""
    pass
