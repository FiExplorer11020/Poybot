"""Canonical registry of ingestion daemons + liveness probes.

Round 6 / The Spine § 3.5. Wave 2b body.

The :class:`DaemonRegistry` enumerates every ingestion daemon the bot
expects to be running on a box and exposes a single liveness check
that the dashboard + the engine's watchdog poll. Discovery uses
systemd's CLI (``systemctl is-active``, ``systemctl show``) via
``asyncio.create_subprocess_exec`` — no shell, hard timeout, and
graceful degradation when any individual query fails.

This module's only side effect (besides spawning short-lived
subprocesses) is updating the three Prometheus metrics in
:mod:`src.monitoring.metrics`:

  * ``polybot_ingestion_daemon_up{service}`` — Gauge, 0/1
  * ``polybot_ingestion_daemon_restarts_total{service}`` — Counter
  * ``polybot_ingestion_daemon_memory_bytes{service}`` — Gauge

It is designed to run either as a small background task inside
``polymarket-engine.service`` (cheaper, one less process) or as its
own standalone process if the operator wants to keep the engine free
of subprocess spawns.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from loguru import logger

from src.monitoring.metrics import (
    ingestion_daemon_memory_bytes,
    ingestion_daemon_restarts_total,
    ingestion_daemon_up,
)


@dataclass(frozen=True)
class DaemonSpec:
    """Static metadata for one ingestion daemon.

    Frozen because the registry is constant at module-import time; any
    addition is a code change.

    Fields:
        name: Short canonical name (matches Prometheus label value and
            the systemd unit's basename minus ``.service``).
        unit_name: Full systemd unit name (e.g. ``polymarket-engine.service``).
        module: Python entrypoint, e.g. ``"src.engine.main"``. Used by
            the supervisor for self-restart paths and as ExecStart in
            the systemd unit.
        memory_max_mb: Memory budget from R6 § 3.5. Wave 2 enforces via
            MemoryMax= in the systemd unit; the supervisor also reads
            this for the
            ``polybot_ingestion_daemon_memory_bytes{service}`` gauge.
    """

    name: str
    unit_name: str
    module: str
    memory_max_mb: int


# Canonical roster — one entry per daemon expected on box-1 in steady
# state. The order matters for the dashboard's "ingestion health" row
# (rendered left-to-right by this order).
CANONICAL_DAEMONS: tuple[DaemonSpec, ...] = (
    DaemonSpec(
        name="engine",
        unit_name="polymarket-engine.service",
        module="src.engine.main",
        memory_max_mb=800,
    ),
    DaemonSpec(
        name="observer",
        unit_name="polymarket-observer.service",
        module="src.observer.main",
        memory_max_mb=400,
    ),
    DaemonSpec(
        name="onchain",
        unit_name="polymarket-onchain.service",
        module="src.onchain.main",
        memory_max_mb=400,
    ),
    DaemonSpec(
        name="crawler",
        unit_name="polymarket-crawler.service",
        module="src.crawler.main",
        memory_max_mb=200,
    ),
    DaemonSpec(
        name="falcon-refresher",
        unit_name="polymarket-falcon-refresher.service",
        # Entrypoint expected at src/registry/refresher_main.py. If not
        # yet present, systemd will mark the unit failed; the supervisor
        # surfaces this via ``is_running == False`` and the dashboard's
        # Bot Health tab makes it obvious.
        module="src.registry.refresher_main",
        memory_max_mb=200,
    ),
    DaemonSpec(
        name="api",
        unit_name="polymarket-api.service",
        module="src.api.main",
        memory_max_mb=300,
    ),
)


# Hard cap on how long a single ``systemctl`` invocation may run. Two
# seconds is generous — the local call typically returns in < 30 ms.
# Anything slower than this and we'd rather report "unknown" than
# block the supervisor loop.
_SUBPROCESS_TIMEOUT_S: float = 2.0


async def _run_systemctl(*args: str) -> tuple[int, str]:
    """Run ``systemctl`` with the given args, return (returncode, stdout).

    Uses ``asyncio.create_subprocess_exec`` (NOT shell) to avoid any
    quoting issues and to keep the attack surface minimal. Stdout is
    decoded as utf-8 and stripped. Returns ``(returncode, "")`` on
    timeout or any unexpected error so callers don't have to
    re-implement defensive try/except in every probe.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "systemctl",
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (FileNotFoundError, PermissionError) as exc:
        # systemctl missing (dev macOS, alpine container) — treat as
        # "no systemd here". Caller will report the daemon as down.
        logger.debug("systemctl not invokable: {}", exc)
        return 1, ""
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("systemctl spawn failed: {}", exc)
        return 1, ""

    try:
        stdout, _stderr = await asyncio.wait_for(
            proc.communicate(), timeout=_SUBPROCESS_TIMEOUT_S
        )
    except asyncio.TimeoutError:
        logger.warning("systemctl {} timed out after {}s", args, _SUBPROCESS_TIMEOUT_S)
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        return 1, ""

    return proc.returncode or 0, stdout.decode("utf-8", errors="replace").strip()


class DaemonRegistry:
    """Liveness + memory snapshot for every canonical daemon.

    The dashboard pulls a snapshot every ~5 s; the engine watchdog
    every ~30 s. Subprocess invocations are issued concurrently via
    :func:`asyncio.gather`, so a full refresh of all six daemons
    (3 probes each, 18 invocations) finishes in well under a second
    even on a loaded box.
    """

    def __init__(
        self, daemons: tuple[DaemonSpec, ...] = CANONICAL_DAEMONS
    ) -> None:
        """
        Args:
            daemons: Override the canonical roster — useful in tests
                and in environments that don't run every daemon (e.g.
                a research box that only runs API + cold_storage).
        """
        self._daemons: tuple[DaemonSpec, ...] = daemons
        self._by_name: dict[str, DaemonSpec] = {d.name: d for d in daemons}
        # Counter is monotonic. We track the last-observed NRestarts
        # value per daemon and only ``inc()`` by the delta. Otherwise
        # every refresh would double-count.
        self._last_restart_seen: dict[str, int] = {}

    @property
    def daemons(self) -> tuple[DaemonSpec, ...]:
        """The roster this registry tracks. Read-only."""
        return self._daemons

    def _spec(self, name: str) -> DaemonSpec:
        try:
            return self._by_name[name]
        except KeyError as exc:
            raise KeyError(f"Unknown ingestion daemon: {name!r}") from exc

    async def is_running(self, name: str) -> bool:
        """Returns True iff the named daemon is currently up per systemd.

        Uses ``systemctl is-active <unit>``. Considers any non-zero
        exit code OR any output other than ``"active"`` as down. This
        intentionally treats ``"activating"``, ``"reloading"``, and
        ``"failed"`` as not running — the supervisor's job is to spot
        crash-loops, and a daemon that's perpetually reloading is just
        as broken as one that's dead.

        Args:
            name: One of the DaemonSpec.name values in
                CANONICAL_DAEMONS.

        Raises:
            KeyError: if ``name`` isn't in the registry.
        """
        spec = self._spec(name)
        rc, out = await _run_systemctl("is-active", spec.unit_name)
        return rc == 0 and out == "active"

    async def memory_bytes(self, name: str) -> int | None:
        """Returns the daemon's current RSS in bytes, or None on failure.

        Uses ``systemctl show -p MemoryCurrent --value <unit>``. Systemd
        returns ``"[not set]"`` for inactive units and a decimal byte
        count otherwise. We return ``None`` on anything we can't parse
        (callers treat that as "skip the gauge update for this tick").

        Args:
            name: One of the DaemonSpec.name values.
        """
        spec = self._spec(name)
        rc, out = await _run_systemctl(
            "show", "-p", "MemoryCurrent", "--value", spec.unit_name
        )
        if rc != 0 or not out:
            return None
        try:
            value = int(out)
        except ValueError:
            # "[not set]" or any other non-numeric sentinel.
            return None
        # Systemd reports 2**64 - 1 when MemoryCurrent is unset on some
        # versions; treat that as "no data" too.
        if value < 0 or value >= 2**63:
            return None
        return value

    async def restart_count(self, name: str) -> int | None:
        """Returns NRestarts as reported by systemd, or None on failure.

        Drives ``polybot_ingestion_daemon_restarts_total{service}``.
        Useful for spotting a daemon stuck in a crash-loop without
        scraping journalctl.
        """
        spec = self._spec(name)
        rc, out = await _run_systemctl(
            "show", "-p", "NRestarts", "--value", spec.unit_name
        )
        if rc != 0 or not out:
            return None
        try:
            return int(out)
        except ValueError:
            return None

    async def _probe_one(self, spec: DaemonSpec) -> dict[str, Any]:
        """Run all three probes for a single daemon concurrently."""
        running, mem, restarts = await asyncio.gather(
            self.is_running(spec.name),
            self.memory_bytes(spec.name),
            self.restart_count(spec.name),
        )
        return {
            "name": spec.name,
            "running": running,
            "memory_bytes": mem,
            "restart_count": restarts,
        }

    async def refresh_all(self) -> dict[str, dict[str, Any]]:
        """Probe every daemon and update Prometheus metrics.

        All ``len(daemons) * 3`` subprocess calls run concurrently
        through :func:`asyncio.gather`; in practice a full refresh
        completes in well under 100 ms on a healthy box.

        Returns:
            ``{name: {running, memory_bytes, restart_count}, ...}`` in
            CANONICAL_DAEMONS order. Used by the dashboard's
            "Bot Health" tab and the /api/inspector/snapshot endpoint.
        """
        results = await asyncio.gather(
            *(self._probe_one(d) for d in self._daemons)
        )
        out: dict[str, dict[str, Any]] = {}
        for spec, result in zip(self._daemons, results):
            running = bool(result["running"])
            mem = result["memory_bytes"]
            restarts = result["restart_count"]

            # Gauge: up/down, always update.
            ingestion_daemon_up.labels(service=spec.name).set(
                1.0 if running else 0.0
            )

            # Gauge: memory — only update when we got a value.
            if mem is not None:
                ingestion_daemon_memory_bytes.labels(service=spec.name).set(
                    float(mem)
                )

            # Counter: only inc by the positive delta against the last
            # observation. NRestarts is monotonic on a single boot but
            # resets to 0 when systemd reloads the unit, so we skip
            # negative deltas (don't reset Prometheus counters; just
            # wait for the next increment).
            if restarts is not None:
                prev = self._last_restart_seen.get(spec.name)
                if prev is None:
                    self._last_restart_seen[spec.name] = restarts
                    if restarts > 0:
                        ingestion_daemon_restarts_total.labels(
                            service=spec.name
                        ).inc(restarts)
                elif restarts > prev:
                    ingestion_daemon_restarts_total.labels(
                        service=spec.name
                    ).inc(restarts - prev)
                    self._last_restart_seen[spec.name] = restarts
                elif restarts < prev:
                    # Unit was reloaded / replaced; rebase without
                    # touching the counter.
                    self._last_restart_seen[spec.name] = restarts

            out[spec.name] = {
                "running": running,
                "memory_bytes": mem,
                "restart_count": restarts,
            }

        return out

    async def snapshot(self) -> list[dict[str, Any]]:
        """Composite view of every daemon's state.

        Convenience wrapper around :meth:`refresh_all` for callers
        that prefer the list-of-dicts shape used by the dashboard
        snapshot endpoint.

        Returns:
            ``[{name, running, memory_bytes, restart_count}, ...]``
            in CANONICAL_DAEMONS order.
        """
        results = await self.refresh_all()
        return [
            {"name": spec.name, **results[spec.name]} for spec in self._daemons
        ]

    async def run_loop(self, interval_s: int = 30) -> None:
        """Periodic refresh loop. Exits cleanly on CancelledError.

        Args:
            interval_s: Seconds between probes. The engine watchdog
                uses 30 s; the dashboard's poll path doesn't go
                through this loop (it calls :meth:`refresh_all`
                on demand with a 5 s TTL cache).
        """
        logger.info(
            "DaemonRegistry.run_loop starting (interval={}s, daemons={})",
            interval_s,
            len(self._daemons),
        )
        try:
            while True:
                try:
                    await self.refresh_all()
                except Exception as exc:
                    logger.exception("DaemonRegistry.refresh_all failed: {}", exc)
                await asyncio.sleep(interval_s)
        except asyncio.CancelledError:
            logger.info("DaemonRegistry.run_loop cancelled, exiting cleanly")
            raise
