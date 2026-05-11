"""Wave-1 smoke test for src.rpc.rate_limiter.

Confirms the module imports without errors. Wave-2 agents will replace
this with real tests covering bucket refill semantics, the unlimited
fast path, and 429-driven penalty windows.
"""

import pytest  # noqa: F401

import src.rpc.rate_limiter  # noqa: F401


def test_module_imports():
    """Smoke test — confirms the module imports without errors. Wave-2
    agents will replace this with real tests."""
    pass
