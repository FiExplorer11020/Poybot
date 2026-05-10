"""
Global killswitch service.

Two-level execution control:
    - execution_enabled       : master switch. If False, neither paper nor real run.
    - real_execution_enabled  : if False (default), only paper runs. If True, both
                                paper and real run (paper always shadows real).

Source of truth is the `system_control` table (singleton row id=1).
Redis caches the state with a short TTL to avoid hammering Postgres on every
trade attempt; on every write we invalidate the cache so flips propagate fast.

Designed to fail SAFE: if Redis or DB are unreachable, the helpers default to
FALSE for both flags (i.e. the bot stops trading). A degraded infra must NEVER
unintentionally enable execution.
"""

from __future__ import annotations

import inspect
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from loguru import logger

from src.database.connection import get_db

REDIS_KEY = "control:killswitch:state"
# S3.9: every state mutation publishes a JSON snapshot here so the
# Telegram bot (and any other observer) can alert on flips. We never
# read this channel — it's a fire-and-forget broadcast.
REDIS_KILLSWITCH_CHANNEL = "control:killswitch_changed"
REDIS_TTL_S = 2  # short — DB is source of truth, cache just absorbs hot read load
DEFAULT_PROCESS_NAME = "polymarket_bot"


@dataclass(frozen=True)
class KillswitchState:
    execution_enabled: bool
    real_execution_enabled: bool
    paused_reason: Optional[str]
    updated_by: str
    updated_at: datetime

    def to_dict(self) -> dict:
        return {
            "execution_enabled": self.execution_enabled,
            "real_execution_enabled": self.real_execution_enabled,
            "paused_reason": self.paused_reason,
            "updated_by": self.updated_by,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "KillswitchState":
        ts = payload.get("updated_at")
        if isinstance(ts, str):
            ts = datetime.fromisoformat(ts)
        return cls(
            execution_enabled=bool(payload["execution_enabled"]),
            real_execution_enabled=bool(payload["real_execution_enabled"]),
            paused_reason=payload.get("paused_reason"),
            updated_by=payload.get("updated_by") or "system",
            updated_at=ts or datetime.now(tz=timezone.utc),
        )


# --------------------------------------------------------------------------- #
# Service                                                                      #
# --------------------------------------------------------------------------- #


class KillswitchService:
    """
    Per-process service. Instantiate once at app startup, share via a module
    singleton or DI. Stateless beyond the redis client reference.
    """

    def __init__(self, redis_client=None):
        self._redis = redis_client

    def attach_redis(self, redis_client) -> None:
        self._redis = redis_client

    # ------------------ public read API ------------------ #

    async def get_state(self) -> KillswitchState:
        """
        Returns the current killswitch state. Reads Redis cache first, falls
        back to DB on miss. On infra failure, returns a SAFE state (everything
        off) and logs the error.
        """
        cached = await self._read_cache()
        if cached is not None:
            return cached

        try:
            state = await self._read_db()
        except Exception as e:
            logger.error(f"killswitch: DB read failed, defaulting to SAFE off: {e}")
            return _safe_off_state(reason=f"infra_failure:{e.__class__.__name__}")

        await self._write_cache(state)
        return state

    async def is_execution_enabled(self) -> bool:
        state = await self.get_state()
        return state.execution_enabled

    async def is_real_execution_enabled(self) -> bool:
        state = await self.get_state()
        # Real execution requires BOTH the master switch AND the real-specific switch.
        return state.execution_enabled and state.real_execution_enabled

    # ------------------ public write API ------------------ #

    async def set_execution_enabled(
        self,
        enabled: bool,
        *,
        reason: Optional[str] = None,
        actor: str = DEFAULT_PROCESS_NAME,
    ) -> KillswitchState:
        return await self._mutate(
            execution_enabled=enabled,
            real_execution_enabled=None,
            reason=reason,
            actor=actor,
        )

    async def set_real_execution_enabled(
        self,
        enabled: bool,
        *,
        reason: Optional[str] = None,
        actor: str = DEFAULT_PROCESS_NAME,
    ) -> KillswitchState:
        return await self._mutate(
            execution_enabled=None,
            real_execution_enabled=enabled,
            reason=reason,
            actor=actor,
        )

    async def set_state(
        self,
        *,
        execution_enabled: Optional[bool] = None,
        real_execution_enabled: Optional[bool] = None,
        reason: Optional[str] = None,
        actor: str = DEFAULT_PROCESS_NAME,
    ) -> KillswitchState:
        return await self._mutate(
            execution_enabled=execution_enabled,
            real_execution_enabled=real_execution_enabled,
            reason=reason,
            actor=actor,
        )

    # ------------------ internals ------------------ #

    async def _mutate(
        self,
        *,
        execution_enabled: Optional[bool],
        real_execution_enabled: Optional[bool],
        reason: Optional[str],
        actor: str,
    ) -> KillswitchState:
        async with get_db() as conn:
            async with conn.transaction():
                current = await conn.fetchrow(
                    "SELECT execution_enabled, real_execution_enabled, paused_reason "
                    "FROM system_control WHERE id = 1 FOR UPDATE"
                )
                if current is None:
                    # Row should exist from migration 006; defensive insert.
                    await conn.execute(
                        "INSERT INTO system_control (id, execution_enabled, "
                        "real_execution_enabled, paused_reason, updated_by) "
                        "VALUES (1, $1, $2, $3, $4)",
                        execution_enabled if execution_enabled is not None else True,
                        real_execution_enabled if real_execution_enabled is not None else False,
                        reason,
                        actor,
                    )
                else:
                    new_exec = (
                        execution_enabled
                        if execution_enabled is not None
                        else bool(current["execution_enabled"])
                    )
                    new_real = (
                        real_execution_enabled
                        if real_execution_enabled is not None
                        else bool(current["real_execution_enabled"])
                    )
                    await conn.execute(
                        "UPDATE system_control "
                        "SET execution_enabled = $1, "
                        "    real_execution_enabled = $2, "
                        "    paused_reason = $3, "
                        "    updated_by = $4, "
                        "    updated_at = NOW() "
                        "WHERE id = 1",
                        new_exec,
                        new_real,
                        reason,
                        actor,
                    )

                    if execution_enabled is not None and bool(current["execution_enabled"]) != new_exec:
                        await self._audit(
                            conn,
                            field="execution_enabled",
                            old=str(bool(current["execution_enabled"])),
                            new=str(new_exec),
                            reason=reason,
                            actor=actor,
                        )
                    if (
                        real_execution_enabled is not None
                        and bool(current["real_execution_enabled"]) != new_real
                    ):
                        await self._audit(
                            conn,
                            field="real_execution_enabled",
                            old=str(bool(current["real_execution_enabled"])),
                            new=str(new_real),
                            reason=reason,
                            actor=actor,
                        )

            row = await conn.fetchrow(
                "SELECT execution_enabled, real_execution_enabled, paused_reason, "
                "       updated_by, updated_at "
                "FROM system_control WHERE id = 1"
            )

        state = KillswitchState(
            execution_enabled=bool(row["execution_enabled"]),
            real_execution_enabled=bool(row["real_execution_enabled"]),
            paused_reason=row["paused_reason"],
            updated_by=row["updated_by"],
            updated_at=row["updated_at"],
        )
        await self._invalidate_cache()
        await self._write_cache(state)
        await self._publish_change(state)
        logger.warning(
            "killswitch updated by={actor} exec={exec_} real={real} reason={reason}",
            actor=actor,
            exec_=state.execution_enabled,
            real=state.real_execution_enabled,
            reason=reason,
        )
        return state

    async def _publish_change(self, state: KillswitchState) -> None:
        """Best-effort broadcast on REDIS_KILLSWITCH_CHANNEL. Used by the
        Telegram notifier (S3.9) to alert on flips. Failure must never
        propagate — the killswitch mutation already succeeded."""
        if self._redis is None:
            return
        try:
            payload = json.dumps(state.to_dict())
            res = self._redis.publish(REDIS_KILLSWITCH_CHANNEL, payload)
            if inspect.isawaitable(res):
                await res
        except Exception as e:
            logger.warning(f"killswitch: redis publish change failed: {e}")

    async def _audit(
        self,
        conn,
        *,
        field: str,
        old: str,
        new: str,
        reason: Optional[str],
        actor: str,
    ) -> None:
        await conn.execute(
            "INSERT INTO system_control_audit "
            "(field_changed, old_value, new_value, reason, changed_by) "
            "VALUES ($1, $2, $3, $4, $5)",
            field,
            old,
            new,
            reason,
            actor,
        )

    async def _read_db(self) -> KillswitchState:
        async with get_db() as conn:
            row = await conn.fetchrow(
                "SELECT execution_enabled, real_execution_enabled, paused_reason, "
                "       updated_by, updated_at "
                "FROM system_control WHERE id = 1"
            )
        if row is None:
            # Defensive — migration should have seeded id=1.
            logger.warning("killswitch: system_control row missing, returning safe-off")
            return _safe_off_state(reason="row_missing")
        return KillswitchState(
            execution_enabled=bool(row["execution_enabled"]),
            real_execution_enabled=bool(row["real_execution_enabled"]),
            paused_reason=row["paused_reason"],
            updated_by=row["updated_by"],
            updated_at=row["updated_at"],
        )

    async def _read_cache(self) -> Optional[KillswitchState]:
        if self._redis is None:
            return None
        try:
            raw = self._redis.get(REDIS_KEY)
            if inspect.isawaitable(raw):
                raw = await raw
        except Exception as e:
            logger.warning(f"killswitch: redis cache read failed: {e}")
            return None
        if not raw:
            return None
        try:
            return KillswitchState.from_dict(json.loads(raw))
        except Exception as e:
            logger.warning(f"killswitch: cache parse failed: {e}")
            return None

    async def _write_cache(self, state: KillswitchState) -> None:
        if self._redis is None:
            return
        try:
            payload = json.dumps(state.to_dict())
            res = self._redis.set(REDIS_KEY, payload, ex=REDIS_TTL_S)
            if inspect.isawaitable(res):
                await res
        except Exception as e:
            logger.warning(f"killswitch: redis cache write failed: {e}")

    async def _invalidate_cache(self) -> None:
        if self._redis is None:
            return
        try:
            res = self._redis.delete(REDIS_KEY)
            if inspect.isawaitable(res):
                await res
        except Exception as e:
            logger.warning(f"killswitch: redis cache invalidate failed: {e}")


def _safe_off_state(*, reason: str) -> KillswitchState:
    return KillswitchState(
        execution_enabled=False,
        real_execution_enabled=False,
        paused_reason=reason,
        updated_by="safe_default",
        updated_at=datetime.now(tz=timezone.utc),
    )


# --------------------------------------------------------------------------- #
# Module singleton — convenient for code that doesn't want to thread the      #
# service through every call (e.g. paper_trader, risk_manager).               #
# --------------------------------------------------------------------------- #

_singleton: Optional[KillswitchService] = None


def get_killswitch(redis_client=None) -> KillswitchService:
    """
    Returns the process-wide KillswitchService singleton. Lazily instantiated.
    Pass the redis client on the first call (typically from FastAPI lifespan
    or worker main()) so subsequent calls don't have to.
    """
    global _singleton
    if _singleton is None:
        _singleton = KillswitchService(redis_client=redis_client)
    elif redis_client is not None and _singleton._redis is None:
        _singleton.attach_redis(redis_client)
    return _singleton


def set_killswitch(service: KillswitchService) -> None:
    """For tests: replace the singleton with a fixture instance."""
    global _singleton
    _singleton = service
