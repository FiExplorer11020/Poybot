"""Wave-1 smoke test for src.cold_storage.duckdb_view.

Confirms the module imports without errors. Wave-2 agents will replace
this with real tests covering view registration against a fixture
Parquet tree and the query-pass-through contract.
"""

import pytest  # noqa: F401

import src.cold_storage.duckdb_view  # noqa: F401


def test_module_imports():
    """Smoke test — confirms the module imports without errors. Wave-2
    agents will replace this with real tests."""
    pass
