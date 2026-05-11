"""Canonical registry of ingestion daemons + liveness probes.

WAVE-1 ARCHITECT SKELETON. Bodies intentionally not implemented; Wave 2
fills them in. See docs/ROUND_6_THE_SPINE.md § 3.5.

The :class:`DaemonRegistry` enumerates every ingestion daemon the bot
expects to be running on a box and exposes a single liveness check
that the dashboard + the engine's watchdog poll. Today's discovery
mechanism is systemd (``systemctl is-active <unit>``) with a PID-file
fallback for non-systemd environments (dev macOS, Docker compose).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


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
        # NEW (Round 6) — entrypoint to be added by Wave 2 in
        # src/onchain/main.py
        module="src.onchain.main",
        memory_max_mb=400,
    ),
    DaemonSpec(
        name="crawler",
        unit_name="polymarket-crawler.service",
        # NEW (Round 6) — entrypoint added by Wave 2 in
        # src/crawler/main.py
        module="src.crawler.main",
        memory_max_mb=200,
    ),
    DaemonSpec(
        name="falcon-refresher",
        unit_name="polymarket-falcon-refresher.service",
        # NEW (Round 6). The event-driven Falcon refresher splits out
        # of the engine; Wave 2 adds src/registry/refresher_main.py.
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


class DaemonRegistry:
    """Liveness + memory snapshot for every canonical daemon.

    The dashboard pulls a snapshot every ~5 s; the engine watchdog
    every ~30 s. Both should be cheap (no spawning shell commands per
    request) — Wave 2 caches the systemd query result for ~2 s.
    """

    def __init__(self, daemons: tuple[DaemonSpec, ...] = CANONICAL_DAEMONS) -> None:
        """
        Args:
            daemons: Override the canonical roster — useful in tests
                and in environments that don't run every daemon (e.g.
                a research box that only runs API + cold_storage).
        """
        # TODO: implement in Wave 2 — see ROUND_6_THE_SPINE.md § 3.5
        raise NotImplementedError("Wave 2 — see ROUND_6_THE_SPINE.md § 3.5")

    @property
    def daemons(self) -> tuple[DaemonSpec, ...]:
        """The roster this registry tracks. Read-only."""
        raise NotImplementedError("Wave 2 — see ROUND_6_THE_SPINE.md § 3.5")

    async def is_running(self, name: str) -> bool:
        """Returns True iff the named daemon is currently up.

        Discovery (Wave 2):
          1. Try ``systemctl is-active <unit>``. If exit code 0,
             return True.
          2. Fall back to PID-file probe at
             ``/var/run/polymarket-bot/<name>.pid``.
          3. Last resort: Redis liveness key
             ``polybot:daemon:heartbeat:<name>`` updated by the daemon
             itself every WATCHDOG_HEARTBEAT_INTERVAL_S.

        Emits ``polybot_ingestion_daemon_up{service}`` gauge update.

        Args:
            name: One of the DaemonSpec.name values in
                CANONICAL_DAEMONS.

        Raises:
            KeyError: if ``name`` isn't in the registry.
        """
        raise NotImplementedError("Wave 2 — see ROUND_6_THE_SPINE.md § 3.5")

    async def memory_bytes(self, name: str) -> int | None:
        """Returns the daemon's current RSS in bytes, or None if it's
        not running.

        Driven by ``systemctl show -p MemoryCurrent <unit>``. Emits
        ``polybot_ingestion_daemon_memory_bytes{service}``.

        Args:
            name: One of the DaemonSpec.name values.
        """
        raise NotImplementedError("Wave 2 — see ROUND_6_THE_SPINE.md § 3.5")

    async def restart_count(self, name: str) -> int:
        """Returns NRestarts as reported by systemd.

        Drives ``polybot_ingestion_daemon_restarts_total{service}``.
        Useful for spotting a daemon stuck in a crash-loop without
        scraping journalctl.
        """
        raise NotImplementedError("Wave 2 — see ROUND_6_THE_SPINE.md § 3.5")

    async def snapshot(self) -> list[dict[str, Any]]:
        """Composite view of every daemon's state.

        Returns:
            ``[{name, running, memory_bytes, restart_count}, ...]``
            in CANONICAL_DAEMONS order. Used by the dashboard's
            "Bot Health" tab and the /api/inspector/snapshot endpoint.
        """
        raise NotImplementedError("Wave 2 — see ROUND_6_THE_SPINE.md § 3.5")
