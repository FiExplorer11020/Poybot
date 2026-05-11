"""Wave-1 smoke test for src.crawler.universe.

Confirms the module imports without errors. Wave-2 agents will replace
this with real tests covering add_wallet_if_new idempotency,
update_activity aggregation correctness, and backfill chunking.
"""

import pytest  # noqa: F401

import src.crawler.universe  # noqa: F401


def test_module_imports():
    """Smoke test — confirms the module imports without errors. Wave-2
    agents will replace this with real tests."""
    pass
