"""Wave-1 smoke test for src.cold_storage.exporter.

Confirms the module imports without errors. Wave-2 agents will replace
this with real tests covering daily partition selection,
atomic-rename invariant, and per-table error isolation.
"""

import pytest  # noqa: F401

import src.cold_storage.exporter  # noqa: F401


def test_module_imports():
    """Smoke test — confirms the module imports without errors. Wave-2
    agents will replace this with real tests."""
    pass
