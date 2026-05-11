"""Unit tests for :class:`src.ingestion_daemon.supervisor.DaemonRegistry`.

The registry is a thin wrapper over ``systemctl`` subprocess calls. The
tests mock :func:`asyncio.create_subprocess_exec` to return canned
stdout / returncode pairs without ever invoking systemd. We assert on
the parsing contract (numeric MemoryCurrent → bytes, ``"[not set]"``
→ None, etc.), on the metric side-effects (Counter only ever
incremented by the positive delta), and on the lifecycle contract
(``run_loop`` exits cleanly on CancelledError).
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.ingestion_daemon.supervisor import (
    CANONICAL_DAEMONS,
    DaemonRegistry,
    DaemonSpec,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_proc(stdout: str = "", returncode: int = 0) -> MagicMock:
    """Build a mock asyncio subprocess returning the given canned output."""
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(
        return_value=(stdout.encode("utf-8"), b"")
    )
    proc.kill = MagicMock()
    return proc


def _make_subprocess_router(responses: dict[tuple[str, ...], MagicMock]):
    """Return an async function that dispatches based on args.

    ``responses`` keys are tuples of systemctl args (without the
    leading ``"systemctl"``). Any args not in the dict default to a
    successful empty response.
    """

    async def _router(cmd: str, *args: str, **_kw: Any) -> MagicMock:
        assert cmd == "systemctl", f"unexpected exec target: {cmd!r}"
        key = tuple(args)
        return responses.get(key, _fake_proc("", 0))

    return _router


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def test_registry_uses_canonical_roster_by_default() -> None:
    registry = DaemonRegistry()
    assert len(registry.daemons) == 6
    assert {d.name for d in registry.daemons} == {
        "engine", "observer", "onchain", "crawler",
        "falcon-refresher", "api",
    }


def test_registry_accepts_custom_roster() -> None:
    spec = DaemonSpec(
        name="custom",
        unit_name="polymarket-custom.service",
        module="src.custom.main",
        memory_max_mb=100,
    )
    registry = DaemonRegistry(daemons=(spec,))
    assert registry.daemons == (spec,)


def test_canonical_memory_budgets_match_r6_spec() -> None:
    """R6 § 3.5 budgets: engine 800 / observer 400 / onchain 400 /
    crawler 200 / falcon-refresher 200 / api 300."""
    budgets = {d.name: d.memory_max_mb for d in CANONICAL_DAEMONS}
    assert budgets == {
        "engine": 800,
        "observer": 400,
        "onchain": 400,
        "crawler": 200,
        "falcon-refresher": 200,
        "api": 300,
    }


# ---------------------------------------------------------------------------
# is_running
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_is_running_true_on_active() -> None:
    registry = DaemonRegistry()
    with patch(
        "src.ingestion_daemon.supervisor.asyncio.create_subprocess_exec",
        AsyncMock(return_value=_fake_proc("active\n", 0)),
    ):
        assert await registry.is_running("engine") is True


@pytest.mark.asyncio
@pytest.mark.parametrize("stdout,rc", [
    ("inactive", 3),
    ("failed", 3),
    ("activating", 0),
    ("reloading", 0),
])
async def test_is_running_false_on_non_active(stdout: str, rc: int) -> None:
    registry = DaemonRegistry()
    with patch(
        "src.ingestion_daemon.supervisor.asyncio.create_subprocess_exec",
        AsyncMock(return_value=_fake_proc(stdout, rc)),
    ):
        assert await registry.is_running("engine") is False


@pytest.mark.asyncio
async def test_is_running_false_on_subprocess_spawn_error() -> None:
    registry = DaemonRegistry()
    with patch(
        "src.ingestion_daemon.supervisor.asyncio.create_subprocess_exec",
        AsyncMock(side_effect=FileNotFoundError("no systemctl")),
    ):
        assert await registry.is_running("engine") is False


@pytest.mark.asyncio
async def test_is_running_false_on_subprocess_timeout() -> None:
    registry = DaemonRegistry()
    proc = MagicMock()
    proc.returncode = None

    async def _hang(*_a: Any, **_kw: Any) -> tuple[bytes, bytes]:
        await asyncio.sleep(10)
        return b"", b""

    proc.communicate = _hang
    proc.kill = MagicMock()

    with patch(
        "src.ingestion_daemon.supervisor.asyncio.create_subprocess_exec",
        AsyncMock(return_value=proc),
    ), patch(
        "src.ingestion_daemon.supervisor._SUBPROCESS_TIMEOUT_S", 0.05
    ):
        assert await registry.is_running("engine") is False
    proc.kill.assert_called_once()


@pytest.mark.asyncio
async def test_is_running_keyerror_on_unknown() -> None:
    registry = DaemonRegistry()
    with pytest.raises(KeyError):
        await registry.is_running("nope")


# ---------------------------------------------------------------------------
# memory_bytes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_memory_bytes_parses_numeric() -> None:
    registry = DaemonRegistry()
    with patch(
        "src.ingestion_daemon.supervisor.asyncio.create_subprocess_exec",
        AsyncMock(return_value=_fake_proc("1234567\n", 0)),
    ):
        assert await registry.memory_bytes("engine") == 1234567


@pytest.mark.asyncio
@pytest.mark.parametrize("stdout", ["[not set]", "infinity", "", "n/a"])
async def test_memory_bytes_none_on_non_numeric(stdout: str) -> None:
    registry = DaemonRegistry()
    with patch(
        "src.ingestion_daemon.supervisor.asyncio.create_subprocess_exec",
        AsyncMock(return_value=_fake_proc(stdout, 0)),
    ):
        assert await registry.memory_bytes("engine") is None


@pytest.mark.asyncio
async def test_memory_bytes_none_on_sentinel_overflow() -> None:
    registry = DaemonRegistry()
    # 2**64 - 1 — systemd's "unset" sentinel on some versions.
    with patch(
        "src.ingestion_daemon.supervisor.asyncio.create_subprocess_exec",
        AsyncMock(return_value=_fake_proc(str(2**64 - 1), 0)),
    ):
        assert await registry.memory_bytes("engine") is None


# ---------------------------------------------------------------------------
# restart_count
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_restart_count_parses_int() -> None:
    registry = DaemonRegistry()
    with patch(
        "src.ingestion_daemon.supervisor.asyncio.create_subprocess_exec",
        AsyncMock(return_value=_fake_proc("7\n", 0)),
    ):
        assert await registry.restart_count("engine") == 7


@pytest.mark.asyncio
async def test_restart_count_none_on_garbage() -> None:
    registry = DaemonRegistry()
    with patch(
        "src.ingestion_daemon.supervisor.asyncio.create_subprocess_exec",
        AsyncMock(return_value=_fake_proc("nope", 0)),
    ):
        assert await registry.restart_count("engine") is None


# ---------------------------------------------------------------------------
# refresh_all — metric side effects
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refresh_all_updates_all_three_gauges_per_daemon() -> None:
    spec = DaemonSpec(
        name="engine",
        unit_name="polymarket-engine.service",
        module="src.engine.main",
        memory_max_mb=800,
    )
    registry = DaemonRegistry(daemons=(spec,))

    responses = {
        ("is-active", "polymarket-engine.service"): _fake_proc("active", 0),
        ("show", "-p", "MemoryCurrent", "--value",
         "polymarket-engine.service"): _fake_proc("536870912", 0),  # 512 MB
        ("show", "-p", "NRestarts", "--value",
         "polymarket-engine.service"): _fake_proc("2", 0),
    }

    with patch(
        "src.ingestion_daemon.supervisor.asyncio.create_subprocess_exec",
        AsyncMock(side_effect=_make_subprocess_router(responses)),
    ), patch(
        "src.ingestion_daemon.supervisor.ingestion_daemon_up"
    ) as up_metric, patch(
        "src.ingestion_daemon.supervisor.ingestion_daemon_memory_bytes"
    ) as mem_metric, patch(
        "src.ingestion_daemon.supervisor.ingestion_daemon_restarts_total"
    ) as restarts_metric:
        result = await registry.refresh_all()

    assert result["engine"] == {
        "running": True,
        "memory_bytes": 536870912,
        "restart_count": 2,
    }
    up_metric.labels.assert_called_with(service="engine")
    up_metric.labels.return_value.set.assert_called_with(1.0)
    mem_metric.labels.assert_called_with(service="engine")
    mem_metric.labels.return_value.set.assert_called_with(536870912.0)
    restarts_metric.labels.assert_called_with(service="engine")
    # First observation seeds the counter by the full value.
    restarts_metric.labels.return_value.inc.assert_called_with(2)


@pytest.mark.asyncio
async def test_refresh_all_counter_increments_by_delta_only() -> None:
    spec = DaemonSpec(
        name="engine",
        unit_name="polymarket-engine.service",
        module="src.engine.main",
        memory_max_mb=800,
    )
    registry = DaemonRegistry(daemons=(spec,))

    # First refresh — NRestarts=3.
    r1 = {
        ("is-active", "polymarket-engine.service"): _fake_proc("active", 0),
        ("show", "-p", "MemoryCurrent", "--value",
         "polymarket-engine.service"): _fake_proc("100", 0),
        ("show", "-p", "NRestarts", "--value",
         "polymarket-engine.service"): _fake_proc("3", 0),
    }
    # Second refresh — NRestarts=5 (delta = 2).
    r2 = dict(r1)
    r2[("show", "-p", "NRestarts", "--value",
        "polymarket-engine.service")] = _fake_proc("5", 0)
    # Third refresh — NRestarts back to 0 (unit reloaded). Counter
    # must NOT be decremented; the next increase rebases from 0.
    r3 = dict(r1)
    r3[("show", "-p", "NRestarts", "--value",
        "polymarket-engine.service")] = _fake_proc("0", 0)

    with patch(
        "src.ingestion_daemon.supervisor.ingestion_daemon_restarts_total"
    ) as restarts_metric:
        with patch(
            "src.ingestion_daemon.supervisor.asyncio.create_subprocess_exec",
            AsyncMock(side_effect=_make_subprocess_router(r1)),
        ):
            await registry.refresh_all()
        with patch(
            "src.ingestion_daemon.supervisor.asyncio.create_subprocess_exec",
            AsyncMock(side_effect=_make_subprocess_router(r2)),
        ):
            await registry.refresh_all()
        with patch(
            "src.ingestion_daemon.supervisor.asyncio.create_subprocess_exec",
            AsyncMock(side_effect=_make_subprocess_router(r3)),
        ):
            await registry.refresh_all()

    inc_calls = [c.args for c in restarts_metric.labels.return_value.inc.call_args_list]
    # First call: 3 (seed). Second: 2 (delta 3→5). Third: nothing (reset).
    assert inc_calls == [(3,), (2,)]


@pytest.mark.asyncio
async def test_refresh_all_skips_memory_gauge_when_none() -> None:
    spec = DaemonSpec(
        name="engine",
        unit_name="polymarket-engine.service",
        module="src.engine.main",
        memory_max_mb=800,
    )
    registry = DaemonRegistry(daemons=(spec,))

    responses = {
        ("is-active", "polymarket-engine.service"): _fake_proc("inactive", 3),
        ("show", "-p", "MemoryCurrent", "--value",
         "polymarket-engine.service"): _fake_proc("[not set]", 0),
        ("show", "-p", "NRestarts", "--value",
         "polymarket-engine.service"): _fake_proc("0", 0),
    }

    with patch(
        "src.ingestion_daemon.supervisor.asyncio.create_subprocess_exec",
        AsyncMock(side_effect=_make_subprocess_router(responses)),
    ), patch(
        "src.ingestion_daemon.supervisor.ingestion_daemon_up"
    ) as up_metric, patch(
        "src.ingestion_daemon.supervisor.ingestion_daemon_memory_bytes"
    ) as mem_metric:
        result = await registry.refresh_all()

    # Down → gauge set to 0.
    up_metric.labels.return_value.set.assert_called_with(0.0)
    # Memory gauge NOT updated when value can't be parsed.
    mem_metric.labels.return_value.set.assert_not_called()
    assert result["engine"]["running"] is False
    assert result["engine"]["memory_bytes"] is None


@pytest.mark.asyncio
async def test_refresh_all_is_concurrent() -> None:
    """6 daemons × 3 probes = 18 subprocess calls; serial would be 18×N ms,
    concurrent should be well under 100 ms even with 20 ms per call."""

    async def _slow_proc(*_a: Any, **_kw: Any) -> MagicMock:
        await asyncio.sleep(0.02)  # 20 ms per call
        return _fake_proc("active", 0)

    registry = DaemonRegistry()
    with patch(
        "src.ingestion_daemon.supervisor.asyncio.create_subprocess_exec",
        AsyncMock(side_effect=_slow_proc),
    ):
        start = time.perf_counter()
        await registry.refresh_all()
        elapsed = time.perf_counter() - start

    # 18 serial × 20 ms = 360 ms. Concurrent should be ~20-50 ms.
    assert elapsed < 0.2, f"refresh_all not concurrent: {elapsed*1000:.0f} ms"


# ---------------------------------------------------------------------------
# snapshot
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_snapshot_returns_list_in_roster_order() -> None:
    registry = DaemonRegistry()
    with patch(
        "src.ingestion_daemon.supervisor.asyncio.create_subprocess_exec",
        AsyncMock(return_value=_fake_proc("active", 0)),
    ):
        snap = await registry.snapshot()

    assert [row["name"] for row in snap] == [d.name for d in CANONICAL_DAEMONS]
    assert all("running" in row for row in snap)


# ---------------------------------------------------------------------------
# run_loop lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_loop_exits_cleanly_on_cancel() -> None:
    registry = DaemonRegistry()
    registry.refresh_all = AsyncMock(return_value={})  # type: ignore[assignment]

    task = asyncio.create_task(registry.run_loop(interval_s=10))
    # Give the loop one event-loop turn to enter refresh_all once.
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_run_loop_swallows_refresh_exceptions() -> None:
    """A transient subprocess error must not kill the loop — the next
    tick gets another chance."""
    registry = DaemonRegistry()
    call_count = 0

    async def _flaky_refresh() -> dict[str, Any]:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("transient")
        return {}

    registry.refresh_all = _flaky_refresh  # type: ignore[assignment]

    task = asyncio.create_task(registry.run_loop(interval_s=0))
    # Let it cycle a few times.
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert call_count >= 2, f"loop died after first failure (calls={call_count})"
