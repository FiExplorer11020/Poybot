"""Tests for src.rpc.circuit_breaker.CircuitBreaker.

Covers:
  * CLOSED -> 5 failures -> OPEN
  * OPEN refuses requests until cooldown
  * Cooldown elapsed -> HALF_OPEN allows ONE probe
  * HALF_OPEN + success -> CLOSED
  * HALF_OPEN + failure -> OPEN (cooldown reset)
  * Prometheus gauge set/cleared on state change
"""

import time

from src.rpc.circuit_breaker import CircuitBreaker, CircuitState


def test_initial_state_is_closed():
    cb = CircuitBreaker("test", failure_threshold=5, cooldown_s=1.0)
    assert cb.state == CircuitState.CLOSED
    assert cb.can_attempt() is True


def test_five_consecutive_failures_open_breaker():
    cb = CircuitBreaker("test", failure_threshold=5, cooldown_s=1.0)
    for _ in range(4):
        cb.record_failure()
    assert cb.state == CircuitState.CLOSED
    cb.record_failure()  # the 5th
    assert cb.state == CircuitState.OPEN


def test_success_resets_failure_count_in_closed():
    cb = CircuitBreaker("test", failure_threshold=5, cooldown_s=1.0)
    cb.record_failure()
    cb.record_failure()
    cb.record_success()
    # Re-running another 4 failures should NOT trip (counter reset).
    for _ in range(4):
        cb.record_failure()
    assert cb.state == CircuitState.CLOSED


def test_open_state_refuses_requests_before_cooldown():
    cb = CircuitBreaker("test", failure_threshold=2, cooldown_s=10.0)
    cb.record_failure()
    cb.record_failure()
    assert cb.state == CircuitState.OPEN
    assert cb.can_attempt() is False
    assert cb.time_until_retry_s() > 0


def test_cooldown_elapsed_allows_one_probe_then_blocks():
    cb = CircuitBreaker("test", failure_threshold=2, cooldown_s=0.05)
    cb.record_failure()
    cb.record_failure()
    assert cb.state == CircuitState.OPEN
    time.sleep(0.1)
    # First call after cooldown: arms HALF_OPEN.
    assert cb.can_attempt() is True
    assert cb.state == CircuitState.HALF_OPEN
    # Second call before the probe resolves: refused.
    assert cb.can_attempt() is False


def test_half_open_success_transitions_to_closed():
    cb = CircuitBreaker("test", failure_threshold=2, cooldown_s=0.05)
    cb.record_failure()
    cb.record_failure()
    time.sleep(0.1)
    cb.can_attempt()  # arm HALF_OPEN
    assert cb.state == CircuitState.HALF_OPEN
    cb.record_success()
    assert cb.state == CircuitState.CLOSED
    assert cb.can_attempt() is True


def test_half_open_failure_transitions_back_to_open():
    cb = CircuitBreaker("test", failure_threshold=2, cooldown_s=0.05)
    cb.record_failure()
    cb.record_failure()
    time.sleep(0.1)
    cb.can_attempt()  # arm HALF_OPEN
    cb.record_failure()
    assert cb.state == CircuitState.OPEN
    # Cooldown should be reset; can_attempt() refuses again.
    assert cb.can_attempt() is False
    assert cb.time_until_retry_s() > 0


def test_force_open_and_close_methods():
    cb = CircuitBreaker("test")
    cb.open()
    assert cb.state == CircuitState.OPEN
    cb.close()
    assert cb.state == CircuitState.CLOSED


def test_prometheus_gauge_reflects_state_changes():
    """Construct a fresh breaker and verify the gauge value matches the
    state transition (0 in CLOSED, 1 in OPEN or HALF_OPEN)."""
    from src.monitoring.metrics import rpc_circuit_breaker_open

    name = f"test-gauge-{time.time()}"  # unique label to avoid sticky state
    cb = CircuitBreaker(name, failure_threshold=2, cooldown_s=0.05)
    # CLOSED -> gauge 0
    g = rpc_circuit_breaker_open.labels(provider=name)
    # Counter / Gauge expose _value.get() in prometheus_client.
    assert g._value.get() == 0.0
    cb.record_failure()
    cb.record_failure()
    assert cb.state == CircuitState.OPEN
    assert g._value.get() == 1.0
    # After successful probe -> CLOSED again, gauge clears.
    time.sleep(0.1)
    cb.can_attempt()
    cb.record_success()
    assert cb.state == CircuitState.CLOSED
    assert g._value.get() == 0.0
