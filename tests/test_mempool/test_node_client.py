"""Smoke test — module import only. Wave-2 will own the real suite.

See docs/ROUND_7_MEMPOOL_AND_PREFILL.md § 3.1 for the contract under test.
"""


def test_module_imports():
    """Importing the module must succeed without side-effects."""
    import src.mempool.node_client  # noqa: F401
