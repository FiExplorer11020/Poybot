"""Round 13 (The Mirror) — Per-model auto / manual disable state.

Audit reference: docs/ROUND_13_CALIBRATION_AND_RESEARCH.md § 3.4.

The auto-disabler is the bot's self-suppression mechanism. When the
drift detector (§ 3.3) accumulates 3+ consecutive days of |z| > 2 for
a (model, strategy_class), this module writes an ``auto`` row in
``model_disable_state``. The confidence engine reads that row before
consulting each model's contribution and skips disabled models.

Suppression mechanism per spec § 3.4:

* ``volume_forecast`` disabled → R9 volume_anticipation policy gated
  off; FOLLOW/FADE continue normally.
* ``causal_ate`` disabled → R10 causal gate is removed; revert to
  pure-Hawkes confidence.
* ``strategy_class`` disabled → R8 conditional weights revert to
  uniform; FOLLOW/FADE remain.
* ``follow_confidence`` is **never auto-disabled** — it's the core
  signal. The drift detector still alerts on it, but the auto-disabler
  refuses to flip its row. The operator can still manually disable via
  Telegram if they really mean to.

Public API:

* ``disable_model(model, reason, auto_or_manual='auto')`` — flip on.
* ``enable_model(model)`` — flip off.
* ``is_disabled(model) -> bool`` — read state (cached briefly).
* ``list_disabled() -> list[dict]`` — for the /disabled Telegram command.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from loguru import logger

from src.database.connection import get_db


# Per spec § 3.4: this model is too central to suppress automatically.
# The drift detector still alerts on it, but the auto-disabler refuses
# to flip its row when called with auto_or_manual='auto'. Manual
# disable from the operator (Telegram /disable) is allowed.
PROTECTED_FROM_AUTO_DISABLE: frozenset[str] = frozenset({"follow_confidence"})

# Read cache TTL — the confidence_engine consults is_disabled() on
# every decision, so we don't want a DB round-trip per trade. 30 s
# matches the runtime_config cache.
_CACHE_TTL_S = 30.0


@dataclass
class DisableState:
    """In-memory mirror of one model_disable_state row."""

    model: str
    is_disabled: bool
    disabled_at: Optional[datetime] = None
    disabled_reason: Optional[str] = None
    auto_or_manual: str = "auto"


class ModelAutoDisabler:
    """Singleton-per-process façade over the model_disable_state table.

    Reads are cached (30 s TTL) to keep the confidence engine's hot
    path off the DB. Writes invalidate the cache atomically.

    The Telegram alert hook is injected via ``notify_fn`` so the
    auto-disable path can page the operator without coupling this
    module to the Telegram bot's import surface.
    """

    def __init__(
        self,
        notify_fn: Optional[callable] = None,  # type: ignore[type-arg]
    ) -> None:
        self._notify_fn = notify_fn
        self._cache: dict[str, DisableState] = {}
        self._cache_fetched_at: float = 0.0
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ #
    # Reads                                                              #
    # ------------------------------------------------------------------ #

    async def _refresh_cache(self) -> None:
        """Load every model_disable_state row into the in-memory cache."""
        try:
            async with get_db() as conn:
                rows = await conn.fetch(
                    """
                    SELECT model, is_disabled, disabled_at,
                           disabled_reason, auto_or_manual
                    FROM model_disable_state
                    """
                )
        except Exception as exc:
            logger.debug(f"ModelAutoDisabler: cache refresh failed: {exc}")
            self._cache_fetched_at = time.monotonic()
            return
        new_cache: dict[str, DisableState] = {}
        for row in rows:
            new_cache[row["model"]] = DisableState(
                model=row["model"],
                is_disabled=bool(row["is_disabled"]),
                disabled_at=row["disabled_at"],
                disabled_reason=row["disabled_reason"],
                auto_or_manual=row["auto_or_manual"] or "auto",
            )
        self._cache = new_cache
        self._cache_fetched_at = time.monotonic()

    async def _ensure_cache(self) -> None:
        if time.monotonic() - self._cache_fetched_at < _CACHE_TTL_S:
            return
        async with self._lock:
            if time.monotonic() - self._cache_fetched_at < _CACHE_TTL_S:
                return
            await self._refresh_cache()

    async def is_disabled(self, model: str) -> bool:
        """Return True iff the given model has an is_disabled=TRUE row.

        Defensive: cache miss / DB miss / no row all return False (the
        safe default — pre-R13 behaviour resumes).
        """
        await self._ensure_cache()
        state = self._cache.get(model)
        return bool(state and state.is_disabled)

    async def list_disabled(self) -> list[dict[str, Any]]:
        """Return every model currently disabled, freshest first."""
        await self._ensure_cache()
        out: list[dict[str, Any]] = []
        for state in self._cache.values():
            if not state.is_disabled:
                continue
            out.append(
                {
                    "model": state.model,
                    "disabled_at": state.disabled_at,
                    "disabled_reason": state.disabled_reason,
                    "auto_or_manual": state.auto_or_manual,
                }
            )
        out.sort(
            key=lambda r: r["disabled_at"] or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )
        return out

    # ------------------------------------------------------------------ #
    # Writes                                                             #
    # ------------------------------------------------------------------ #

    async def disable_model(
        self,
        model: str,
        reason: str,
        auto_or_manual: str = "auto",
    ) -> bool:
        """Flip a model's row to is_disabled=TRUE.

        Returns True if the write happened. Returns False if the model
        is in ``PROTECTED_FROM_AUTO_DISABLE`` AND ``auto_or_manual`` is
        'auto' (the spec § 3.4 emergency-state guard for
        follow_confidence). Manual disable from the operator always
        succeeds.
        """
        model = (model or "").strip()
        if not model:
            return False
        if auto_or_manual not in {"auto", "manual"}:
            auto_or_manual = "auto"
        if (
            auto_or_manual == "auto"
            and model in PROTECTED_FROM_AUTO_DISABLE
        ):
            logger.warning(
                f"ModelAutoDisabler: refusing to auto-disable {model!r} "
                "(protected core-signal model). Emergency alert only."
            )
            await self._notify_emergency(model, reason)
            return False

        try:
            async with get_db() as conn:
                await conn.execute(
                    """
                    INSERT INTO model_disable_state
                        (model, is_disabled, disabled_at,
                         disabled_reason, auto_or_manual)
                    VALUES ($1, TRUE, NOW(), $2, $3)
                    ON CONFLICT (model) DO UPDATE
                        SET is_disabled = TRUE,
                            disabled_at = NOW(),
                            disabled_reason = EXCLUDED.disabled_reason,
                            auto_or_manual = EXCLUDED.auto_or_manual
                    """,
                    model,
                    (reason or "")[:200],
                    auto_or_manual,
                )
        except Exception as exc:
            logger.warning(
                f"ModelAutoDisabler: disable write failed for {model!r}: {exc}"
            )
            return False
        # Bust the cache so the next is_disabled() call sees the new row.
        self._cache_fetched_at = 0.0
        await self._inc_disable_metric(model, auto_or_manual)
        logger.info(
            f"ModelAutoDisabler: {model} DISABLED "
            f"({auto_or_manual}) reason={reason!r}"
        )
        await self._notify_change(model, "disabled", reason, auto_or_manual)
        return True

    async def enable_model(self, model: str) -> bool:
        """Flip a model's row to is_disabled=FALSE.

        Returns True if the row was found and updated, False if the
        row didn't exist (no-op — pre-R13 default is already enabled).
        """
        model = (model or "").strip()
        if not model:
            return False
        try:
            async with get_db() as conn:
                result = await conn.execute(
                    """
                    UPDATE model_disable_state
                       SET is_disabled = FALSE,
                           disabled_at = NULL,
                           disabled_reason = NULL,
                           auto_or_manual = 'manual'
                     WHERE model = $1
                    """,
                    model,
                )
        except Exception as exc:
            logger.warning(
                f"ModelAutoDisabler: enable write failed for {model!r}: {exc}"
            )
            return False
        self._cache_fetched_at = 0.0
        await self._inc_enable_metric(model)
        logger.info(f"ModelAutoDisabler: {model} ENABLED")
        await self._notify_change(model, "enabled", None, "manual")
        # asyncpg returns 'UPDATE N' — count > 0 means a row matched.
        try:
            n = int(str(result).split()[-1])
        except Exception:
            n = 1
        return n > 0

    # ------------------------------------------------------------------ #
    # Metrics + alerts                                                   #
    # ------------------------------------------------------------------ #

    @staticmethod
    async def _inc_disable_metric(model: str, auto_or_manual: str) -> None:
        try:
            from src.monitoring import metrics as mm

            if auto_or_manual == "manual":
                mm.model_manual_disable_total.labels(model=model).inc()
            else:
                mm.model_auto_disable_total.labels(model=model).inc()
            mm.model_disabled.labels(model=model).set(1.0)
        except Exception:
            pass

    @staticmethod
    async def _inc_enable_metric(model: str) -> None:
        try:
            from src.monitoring import metrics as mm

            mm.model_enable_total.labels(model=model).inc()
            mm.model_disabled.labels(model=model).set(0.0)
        except Exception:
            pass

    async def _notify_change(
        self,
        model: str,
        action: str,
        reason: Optional[str],
        auto_or_manual: str,
    ) -> None:
        if self._notify_fn is None:
            return
        try:
            msg = (
                f"Model {action}: {model} ({auto_or_manual})"
                + (f" — {reason}" if reason else "")
            )
            await self._notify_fn(msg)
        except Exception as exc:
            logger.debug(f"ModelAutoDisabler: notify_fn raised: {exc}")

    async def _notify_emergency(self, model: str, reason: str) -> None:
        """Emergency alert when a protected model would have been
        auto-disabled. Per spec § 3.4 the bot must NOT auto-suppress
        follow_confidence, but the operator still needs to know that
        the core signal is misbehaving.
        """
        if self._notify_fn is None:
            return
        try:
            msg = (
                f"CRITICAL: protected model {model} hit auto-disable "
                f"threshold but auto-suppression refused. reason={reason}"
            )
            await self._notify_fn(msg)
        except Exception as exc:
            logger.debug(f"ModelAutoDisabler: emergency notify raised: {exc}")


# ---------------------------------------------------------------------------
# Module-level singleton plumbing (mirrors runtime_config / killswitch)
# ---------------------------------------------------------------------------

_auto_disabler: ModelAutoDisabler | None = None


def init_auto_disabler(
    notify_fn: Optional[callable] = None,  # type: ignore[type-arg]
) -> ModelAutoDisabler:
    global _auto_disabler
    _auto_disabler = ModelAutoDisabler(notify_fn=notify_fn)
    return _auto_disabler


def get_auto_disabler() -> ModelAutoDisabler:
    """Return the process-wide singleton. Initialises a bootless
    instance (no notify_fn) on first call if needed — useful for
    unit tests and for very early reads during app startup."""
    global _auto_disabler
    if _auto_disabler is None:
        _auto_disabler = ModelAutoDisabler(notify_fn=None)
    return _auto_disabler


__all__ = [
    "DisableState",
    "ModelAutoDisabler",
    "PROTECTED_FROM_AUTO_DISABLE",
    "get_auto_disabler",
    "init_auto_disabler",
]
