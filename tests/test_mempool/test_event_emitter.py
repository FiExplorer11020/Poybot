"""Smoke test for src.mempool.event_emitter — Wave-2 replaces with real tests."""
import pytest


def test_module_imports():
    """Confirms the module imports without errors. Wave-2 agents will
    replace this with real tests for LeaderIntentPublisher.publish() —
    payload shape (trace_id, published_at_ms), StreamProducer wiring,
    reconnect semantics."""
    from src.mempool import event_emitter  # noqa: F401
