"""Wave-1 smoke test for src.rpc.circuit_breaker.

Confirms the module imports without errors. Wave-2 agents will replace
this with real tests covering the CLOSED → OPEN → HALF_OPEN → CLOSED
state machine and cooldown timer behaviour.
"""

import pytest  # noqa: F401

import src.rpc.circuit_breaker  # noqa: F401


def test_module_imports():
    """Smoke test — confirms the module imports without errors. Wave-2
    agents will replace this with real tests."""
    pass
