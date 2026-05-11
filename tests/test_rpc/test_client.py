"""Wave-1 smoke test for src.rpc.client.

Confirms the module imports without errors. Wave-2 agents will replace
this with real tests covering provider fallback, eth_subscribe
reconnection, in-flight coalescing, and timeout handling.
"""

import pytest  # noqa: F401  (Wave 2 will use pytest decorators)

import src.rpc.client  # noqa: F401


def test_module_imports():
    """Smoke test — confirms the module imports without errors. Wave-2
    agents will replace this with real tests."""
    pass
