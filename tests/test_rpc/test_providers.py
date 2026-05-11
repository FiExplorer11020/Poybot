"""Wave-1 smoke test for src.rpc.providers.

Confirms the module imports without errors. Wave-2 agents will replace
this with real tests covering ProviderPool round-robin selection,
ProviderState lifecycle, and health-check loop behaviour.
"""

import pytest  # noqa: F401

import src.rpc.providers  # noqa: F401


def test_module_imports():
    """Smoke test — confirms the module imports without errors. Wave-2
    agents will replace this with real tests."""
    pass
