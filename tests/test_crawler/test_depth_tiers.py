"""Wave-1 smoke test for src.crawler.depth_tiers.

Confirms the module imports without errors. Wave-2 agents will replace
this with real tests covering expected_tier policy edge cases (the
threshold boundaries especially) and the nightly review_tiers loop.
"""

import pytest  # noqa: F401

import src.crawler.depth_tiers  # noqa: F401


def test_module_imports():
    """Smoke test — confirms the module imports without errors. Wave-2
    agents will replace this with real tests."""
    pass
