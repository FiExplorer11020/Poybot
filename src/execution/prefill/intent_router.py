"""Consume ``mempool:leader_intent`` and route to fire or paper.

Round 7 / The Front Door — § 3.6 + § 3.7.

The IntentRouter is the second half of R7's hot path: it consumes
:class:`src.mempool.tx_decoder.LeaderIntent` events from the
``mempool:leader_intent`` Redis Stream (consumer group
``prefill_router``), applies a strict-order risk decision tree, and
either:

* fires a pre-signed order via :class:`PreSignedPool.fire`
  (LIVE mode, gated by both the global killswitch AND a R7-specific
  ``prefill_live_enabled`` runtime config flag), OR
* opens a paper position via :class:`src.engine.paper_trader.PaperTrader.open_trade`
  (SHADOW mode — default for the first 30 days of operation).

See ``docs/ROUND_7_MEMPOOL_AND_PREFILL.md`` § 3.6 + § 3.7 for the
spec.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Optional

from loguru import logger

from src.config import settings
from src.control.redis_streams import StreamConsumer
from src.database.connection import get_db

if TYPE_CHECKING:  # pragma: no cover — type-only imports
    from src.execution.prefill.pool import PreSignedPool
    from src.mempool.tx_decoder import LeaderIntent


STREAM_NAME = "mempool:leader_intent"
CONSUMER_GROUP = "prefill_router"
DEFAULT_CONSUMER_NAME = "prefill_router.1"

# Result label vocabulary. Must stay in sync with the CHECK constraint
# on mempool_observations.fire_result (migration 024) AND with the
# decisions_total metric's documented label set in src/monitoring/metrics.py.
# 'error' is metric-only — uncaught exceptions skip the observation
# INSERT because the schema would reject the row.
RESULT_FILLED = "filled"
RESULT_POOL_MISS = "pool_miss"
RESULT_KILLSWITCH_OFF = "killswitch_off"
RESULT_RISK_BLOCKED = "risk_blocked"
RESULT_COOLDOWN = "cooldown"
RESULT_CONFIDENCE_SKIP = "confidence_skip"
RESULT_SIZE_CAP = "size_cap"
RESULT_SHADOW = "shadow"
RESULT_ERROR = "error"  # metric-only

# Result labels writable to mempool_observations.fire_result. Stays in
# strict sync with the CHECK constraint in migration 024.
_OBSERVABLE_RESULTS = frozenset(
    {
        RESULT_FILLED,
        RESULT_POOL_MISS,
        RESULT_KILLSWITCH_OFF,
        RESULT_RISK_BLOCKED,
        RESULT_COOLDOWN,
        RESULT_CONFIDENCE_SKIP,
        RESULT_SIZE_CAP,
        RESULT_SHADOW,
    }
)


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


def _parse_intent_received_at(value: Any) -> datetime:
    """Parse the ``intent_received_at`` field off a JSON-decoded payload.

    Redis Streams payloads are JSON, so datetimes arrive as ISO strings.
    A live :class:`LeaderIntent` dataclass passed through the in-process
    path keeps the native :class:`datetime`. Handle both.
    """
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return _utcnow()


class IntentRouter:
    """Stream consumer + decision-tree gate for the prefill path.

    Owns:
      * a :class:`src.control.redis_streams.StreamConsumer` bound to
        ``mempool:leader_intent`` with group ``prefill_router``.
      * references to the pool / live_trader / paper_trader /
        confidence_engine / risk_manager / killswitch collaborators.
      * a background ``pool.expire_stale`` rotation task.
    """

    def __init__(
        self,
        pool: "PreSignedPool",
        live_trader,
        paper_trader,
        confidence_engine,
        risk_manager,
        killswitch,
        *,
        runtime_config: Any = None,
        redis_url: Optional[str] = None,
        stream_name: str = STREAM_NAME,
        consumer_group: str = CONSUMER_GROUP,
        consumer_name: str = DEFAULT_CONSUMER_NAME,
    ) -> None:
        """Bind to all collaborators. See the module docstring for the
        responsibilities of each."""
        self._pool = pool
        self._live_trader = live_trader
        self._paper_trader = paper_trader
        self._confidence_engine = confidence_engine
        self._risk_manager = risk_manager
        self._killswitch = killswitch
        self._runtime_config = runtime_config

        self._stream_name = stream_name
        self._consumer_group = consumer_group
        self._consumer_name = consumer_name
        self._redis_url = redis_url or settings.REDIS_URL

        self._consumer: Optional[StreamConsumer] = None
        self._rotation_task: Optional[asyncio.Task] = None
        self._running = False
        # Cached current capital so we can re-check the position-size
        # gate without round-tripping every intent. PaperTrader.capital
        # is the source of truth.
        self._fallback_capital = float(settings.PAPER_CAPITAL_USDC)

    # ------------------------------------------------------------------ #
    # Lifecycle                                                           #
    # ------------------------------------------------------------------ #

    async def start(self) -> None:
        """Spin up the consumer + background rotation task. Idempotent."""
        if self._running:
            return
        self._consumer = StreamConsumer(
            self._redis_url,
            stream=self._stream_name,
            group=self._consumer_group,
            consumer_name=self._consumer_name,
        )
        self._consumer.register(self._on_stream_entry)
        await self._consumer.start()
        self._rotation_task = asyncio.create_task(
            self._rotation_loop(),
            name="intent_router.pool_rotation",
        )
        self._running = True
        logger.info(
            f"IntentRouter started: stream={self._stream_name} "
            f"group={self._consumer_group} consumer={self._consumer_name}"
        )

    async def stop(self) -> None:
        """Cancel background tasks + close the consumer. Idempotent."""
        if not self._running:
            return
        self._running = False
        if self._rotation_task is not None:
            self._rotation_task.cancel()
            try:
                await self._rotation_task
            except (asyncio.CancelledError, Exception):
                pass
            self._rotation_task = None
        if self._consumer is not None:
            try:
                await self._consumer.stop()
            except Exception:
                pass
            self._consumer = None
        logger.info("IntentRouter stopped")

    async def _rotation_loop(self) -> None:
        """Periodic ``pool.expire_stale`` driver — owned here because the
        pool has no asyncio lifecycle of its own."""
        interval = float(
            getattr(settings, "PREFILL_ROTATION_INTERVAL_S", 30.0)
        )
        while self._running:
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                return
            if not self._running:
                return
            try:
                await self._pool.expire_stale()
            except Exception as exc:
                logger.warning(f"IntentRouter: expire_stale failed: {exc!r}")

    # ------------------------------------------------------------------ #
    # Stream handler                                                      #
    # ------------------------------------------------------------------ #

    async def _on_stream_entry(
        self, payload: dict, _stream: str, _entry_id: str
    ) -> None:
        """StreamConsumer entry point. Reconstructs the LeaderIntent and
        delegates to :meth:`_on_intent`.

        Exceptions are swallowed (and counted) so a buggy collaborator
        cannot crash the consumer loop. A re-raise would force the
        consumer to mark the entry PENDING and retry, but the intent
        is time-sensitive — retrying a stale intent has zero value.
        """
        try:
            intent = self._rehydrate_intent(payload)
        except Exception as exc:
            self._inc_decision(RESULT_ERROR)
            logger.exception(
                f"IntentRouter: failed to rehydrate intent payload: {exc!r}"
            )
            return
        await self._on_intent(intent)

    @staticmethod
    def _rehydrate_intent(payload: dict) -> "LeaderIntent":
        """Reconstruct :class:`LeaderIntent` from a JSON-decoded stream
        payload. Tolerant of either the dataclass shape or a plain dict.
        """
        from src.mempool.tx_decoder import LeaderIntent

        return LeaderIntent(
            intent_id=str(payload["intent_id"]),
            wallet=str(payload["wallet"]),
            market_id=str(payload["market_id"]),
            token_id=str(payload["token_id"]),
            side=payload["side"],
            size_usdc=Decimal(str(payload["size_usdc"])),
            price=Decimal(str(payload["price"])),
            order_type=str(payload.get("order_type", "GTC")),
            intent_received_at=_parse_intent_received_at(
                payload.get("intent_received_at")
            ),
            expected_block=int(payload.get("expected_block") or 0),
            tx_hash=str(payload["tx_hash"]),
            nonce=int(payload.get("nonce") or 0),
            replaces=payload.get("replaces"),
        )

    # ------------------------------------------------------------------ #
    # Decision tree                                                       #
    # ------------------------------------------------------------------ #

    async def _on_intent(self, intent: "LeaderIntent") -> None:
        """Apply the R7 § 3.6 decision tree to a single intent.

        See module docstring for the spec. Exception handling at the
        outer layer keeps the consumer alive on a buggy collaborator.
        """
        start_monotonic = time.monotonic()
        observe_latency = True
        try:
            # 1. Killswitch strict-path consult (Phase 0 R2 B: bypass cache).
            #    Applies in BOTH shadow and live mode — a killswitch-off
            #    state should not even paper-shadow.
            try:
                live_enabled_master = (
                    await self._killswitch.is_real_execution_enabled(
                        bypass_cache=True
                    )
                )
            except Exception as exc:
                logger.error(
                    f"IntentRouter: killswitch strict-path read failed: {exc!r} — "
                    "treating as OFF"
                )
                live_enabled_master = False
            if not live_enabled_master:
                await self._finalize(
                    intent, RESULT_KILLSWITCH_OFF, start_monotonic
                )
                return

            # 2. Confidence-engine gate. The router consults a
            #    `recommend()` API on the engine (the engine's public
            #    `evaluate(trade)` is keyed on a trade dict, not on
            #    a leader+market lookup — Wave-2 adds a thin
            #    `recommend(wallet, market_id)` accessor; until then,
            #    fall back to "FOLLOW" if the method is absent so the
            #    integration tests don't 500 on a partially-implemented
            #    engine).
            try:
                rec = await self._confidence_recommend(intent)
            except Exception as exc:
                logger.warning(
                    f"IntentRouter: confidence engine raised for "
                    f"wallet={intent.wallet[:10]} market={intent.market_id[:10]}: "
                    f"{exc!r} — skipping intent"
                )
                await self._finalize(
                    intent, RESULT_CONFIDENCE_SKIP, start_monotonic
                )
                return
            if rec not in {"follow", "volume_anticipation"}:
                await self._finalize(
                    intent, RESULT_CONFIDENCE_SKIP, start_monotonic
                )
                return

            # 3. Position-size cap. Re-checked here because the prefill
            #    path skips the post-decision RiskManager gate. The
            #    cap matches `risk_per_trade_pct` from runtime_config so
            #    a cockpit flip on the cockpit propagates here too.
            capital = self._current_capital()
            cap_pct = await self._effective_size_cap_pct()
            cap_usdc = Decimal(str(capital)) * Decimal(str(cap_pct))
            if intent.size_usdc > cap_usdc:
                await self._finalize(
                    intent, RESULT_SIZE_CAP, start_monotonic
                )
                return

            # 4. Cooldown gate. Re-uses the existing RiskManager cooldown
            #    ledger. Defensive: if RiskManager doesn't expose
            #    `in_cooldown` (still-on-the-roadmap), skip the check
            #    rather than fail-open by erroring out.
            in_cooldown_fn = getattr(self._risk_manager, "in_cooldown", None)
            if in_cooldown_fn is not None:
                try:
                    cooled = await in_cooldown_fn(
                        intent.wallet, intent.market_id
                    )
                except Exception as exc:
                    logger.warning(
                        f"IntentRouter: risk_manager.in_cooldown raised: "
                        f"{exc!r} — assuming not in cooldown"
                    )
                    cooled = False
                if cooled:
                    await self._finalize(
                        intent, RESULT_COOLDOWN, start_monotonic
                    )
                    return

            # 5. SHADOW vs LIVE branching. Default = shadow.
            live_enabled = await self._prefill_live_enabled()
            if not live_enabled:
                await self._fire_shadow(intent, start_monotonic)
                return

            # 5b. TOCTOU defence — re-consult the killswitch immediately
            #     before firing. Between step 1 and the pool fire we may
            #     have crossed the 2 s cache window during which an
            #     operator flipped the switch.
            try:
                still_enabled = (
                    await self._killswitch.is_real_execution_enabled(
                        bypass_cache=True
                    )
                )
            except Exception as exc:
                logger.error(
                    f"IntentRouter: TOCTOU killswitch re-read failed: {exc!r} — "
                    "treating as OFF"
                )
                still_enabled = False
            if not still_enabled:
                await self._finalize(
                    intent, RESULT_KILLSWITCH_OFF, start_monotonic
                )
                return

            await self._fire_live(intent, start_monotonic)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Defence in depth — a bug anywhere in the decision tree
            # must not bring down the consumer loop.
            observe_latency = False
            self._inc_decision(RESULT_ERROR)
            logger.exception(
                f"IntentRouter: unhandled exception in _on_intent "
                f"(intent_id={getattr(intent, 'intent_id', '?')}): {exc!r}"
            )
        finally:
            if observe_latency:
                self._observe_latency(start_monotonic, intent)

    # ------------------------------------------------------------------ #
    # Branch handlers                                                     #
    # ------------------------------------------------------------------ #

    async def _fire_shadow(
        self, intent: "LeaderIntent", start_monotonic: float
    ) -> None:
        """Open a paper position mirroring the leader's intent. Default
        path during the 30-day soak."""
        decision = self._build_paper_decision(intent)
        try:
            await self._paper_trader.open_trade(decision)
        except Exception as exc:
            # A paper-trade failure is recoverable — log and still
            # record the row as 'shadow' so the soak metrics show the
            # intended branch (the failure is visible elsewhere via
            # PaperTrader's own refusal log).
            logger.warning(
                f"IntentRouter: paper_trader.open_trade raised for "
                f"intent_id={intent.intent_id}: {exc!r}"
            )
        await self._finalize(intent, RESULT_SHADOW, start_monotonic)

    async def _fire_live(
        self, intent: "LeaderIntent", start_monotonic: float
    ) -> None:
        """Hot-path fire via the pre-signed pool."""
        try:
            filled = await self._pool.fire(intent)
        except Exception as exc:
            logger.warning(
                f"IntentRouter: pool.fire raised for "
                f"intent_id={intent.intent_id}: {exc!r}"
            )
            filled = None

        if filled is None:
            await self._finalize(intent, RESULT_POOL_MISS, start_monotonic)
            return

        # Optional: also paper-shadow the fill so the operator has a
        # continuous A/B comparison. The PaperTrader is the canonical
        # sink for the shadow leg.
        shadow_record = getattr(
            self._paper_trader, "shadow_record_fill", None
        )
        if shadow_record is not None:
            try:
                await shadow_record(intent, filled)
            except Exception as exc:  # pragma: no cover — best-effort
                logger.debug(
                    f"IntentRouter: shadow_record_fill skipped: {exc!r}"
                )

        await self._finalize(intent, RESULT_FILLED, start_monotonic)

    # ------------------------------------------------------------------ #
    # Persistence + metrics                                               #
    # ------------------------------------------------------------------ #

    async def _finalize(
        self,
        intent: "LeaderIntent",
        result: str,
        start_monotonic: float,
    ) -> None:
        """Record the observation row, bump the decisions counter and
        observe the latency histogram."""
        elapsed_ms = int((time.monotonic() - start_monotonic) * 1000)
        if result in _OBSERVABLE_RESULTS:
            try:
                await self._insert_observation(intent, result, elapsed_ms)
            except Exception as exc:
                logger.warning(
                    f"IntentRouter: mempool_observations INSERT failed for "
                    f"intent_id={intent.intent_id}: {exc!r}"
                )
        self._inc_decision(result)
        # Latency histogram is observed once per intent by the outer
        # ``_on_intent`` finally-block — see ROUND_7_MEMPOOL_AND_PREFILL.md
        # § 5 ``polybot_intent_router_latency_seconds``. Observing again
        # here would double-count every happy-path intent and bias the
        # p50/p99 quantile estimates used for the § 6 acceptance gate.

    async def _insert_observation(
        self,
        intent: "LeaderIntent",
        result: str,
        latency_ms: int,
    ) -> None:
        """UPSERT a row into ``mempool_observations``.

        UPSERT semantics: the row is keyed by ``intent_id`` (UUID PK).
        On the first sighting we INSERT; on a replay (the stream
        consumer is at-least-once) we leave the existing row alone
        rather than overwriting — the first decision is the authoritative
        one, and an ``ON CONFLICT DO NOTHING`` keeps the path idempotent
        without needing a transaction.
        """
        intent_uuid = _coerce_uuid(intent.intent_id)
        fired_at = _utcnow()
        async with get_db() as conn:
            await conn.execute(
                """
                INSERT INTO mempool_observations
                    (intent_id, wallet_address, market_id, token_id, side,
                     size_usdc, intent_received_at, tx_hash, nonce,
                     replaces_tx_hash, expected_block,
                     fired_at, fire_result, latency_ms_to_fire)
                VALUES
                    ($1, $2, $3, $4, $5,
                     $6, $7, $8, $9,
                     $10, $11,
                     $12, $13, $14)
                ON CONFLICT (intent_id) DO NOTHING
                """,
                intent_uuid,
                intent.wallet,
                intent.market_id,
                intent.token_id,
                intent.side,
                intent.size_usdc,
                intent.intent_received_at,
                intent.tx_hash,
                int(intent.nonce),
                intent.replaces,
                int(intent.expected_block) if intent.expected_block else None,
                fired_at,
                result,
                int(latency_ms),
            )

    def _inc_decision(self, result: str) -> None:
        try:
            from src.monitoring.metrics import intent_router_decisions_total

            intent_router_decisions_total.labels(result=result).inc()
        except Exception:  # pragma: no cover — metrics never crash callers
            pass

    def _observe_latency(
        self, start_monotonic: float, intent: "LeaderIntent"
    ) -> None:
        """Observe the intent_received → fire-complete latency.

        Uses ``intent_received_at`` as t=0 when available — that matches
        the architect's contract on
        ``polybot_intent_router_latency_seconds`` (the histogram is
        keyed on subscription-to-fire, NOT handler-entry-to-fire). Falls
        back to the in-process monotonic delta if the timestamp is
        missing / in the future.
        """
        try:
            from src.monitoring.metrics import intent_router_latency_seconds

            now = _utcnow()
            received_at = intent.intent_received_at
            if received_at is None:
                elapsed = time.monotonic() - start_monotonic
            else:
                if received_at.tzinfo is None:
                    received_at = received_at.replace(tzinfo=timezone.utc)
                elapsed = (now - received_at).total_seconds()
                if elapsed < 0:
                    # Clock skew or test fixture; fall back to monotonic.
                    elapsed = time.monotonic() - start_monotonic
            intent_router_latency_seconds.observe(elapsed)
        except Exception:  # pragma: no cover — metrics never crash callers
            pass

    # ------------------------------------------------------------------ #
    # Config / collaborator adapters                                      #
    # ------------------------------------------------------------------ #

    async def _prefill_live_enabled(self) -> bool:
        """Resolve the shadow-vs-live flag.

        Preferred source: RuntimeConfig key ``prefill_live_enabled``.
        Fallback: ``settings.PREFILL_LIVE_ENABLED`` (env-driven, default
        ``False``).

        TODO(round-7-followup): register ``prefill_live_enabled`` in
        RuntimeConfig.ALLOWED_KEYS with a bool coercion path so the
        cockpit can flip it at runtime. The current registry only
        supports int/float coercion; extending it is a small but
        cross-cutting change deliberately deferred to keep this PR
        focused.
        """
        if self._runtime_config is not None:
            try:
                getter = getattr(self._runtime_config, "get", None)
                if getter is not None:
                    value = await getter("prefill_live_enabled")
                    if value is not None:
                        return bool(value)
            except Exception as exc:
                logger.debug(
                    f"IntentRouter: runtime_config.get('prefill_live_enabled') "
                    f"failed: {exc!r}"
                )
        return bool(getattr(settings, "PREFILL_LIVE_ENABLED", False))

    async def _effective_size_cap_pct(self) -> float:
        """Resolve the per-trade size cap (% of capital).

        Prefers the cockpit-flippable ``risk_per_trade_pct`` over the
        env-driven ``MAX_POSITION_PCT`` so the prefill path tracks
        the same risk envelope as the FOLLOW path.
        """
        if self._runtime_config is not None:
            try:
                getter = getattr(self._runtime_config, "get", None)
                if getter is not None:
                    value = await getter("risk_per_trade_pct")
                    if value is not None:
                        return float(value)
            except Exception as exc:
                logger.debug(
                    f"IntentRouter: runtime_config.get('risk_per_trade_pct') "
                    f"failed: {exc!r}"
                )
        return float(getattr(settings, "MAX_POSITION_PCT", 0.02))

    def _current_capital(self) -> float:
        """Read the paper trader's running capital. Falls back to
        ``settings.PAPER_CAPITAL_USDC`` if the trader hasn't been
        hydrated yet (e.g. on the very first intent before
        :meth:`PaperTrader.load_persisted_state`)."""
        cap_attr = getattr(self._paper_trader, "capital", None)
        try:
            if cap_attr is not None:
                return float(cap_attr)
        except (TypeError, ValueError):
            pass
        return self._fallback_capital

    async def _confidence_recommend(self, intent: "LeaderIntent") -> str:
        """Adapter over the confidence engine.

        Prefers an explicit ``recommend(wallet, market_id)`` method if the
        engine exposes one (Wave-2 may add it as a thin accessor over
        the Thompson posteriors). Falls back to the public
        ``evaluate(trade)`` API with a minimal synthetic trade dict.
        Returns one of the action strings the engine emits
        (``"follow"`` / ``"fade"`` / ``"skip"`` / ``"volume_anticipation"``)
        or ``"skip"`` on a None response.
        """
        recommend = getattr(self._confidence_engine, "recommend", None)
        if recommend is not None:
            result = await recommend(intent.wallet, intent.market_id)
            return _extract_action(result)

        # Fallback: synthesize the minimum trade dict the existing
        # `evaluate` API expects.
        trade = {
            "wallet_address": intent.wallet,
            "market_id": intent.market_id,
            "token_id": intent.token_id,
            "side": intent.side,
            "price": float(intent.price),
            "size_usdc": float(intent.size_usdc),
            "time": intent.intent_received_at.isoformat()
            if intent.intent_received_at
            else _utcnow().isoformat(),
            "is_leader": True,
            "source": "mempool",
        }
        result = await self._confidence_engine.evaluate(trade)
        return _extract_action(result)

    def _build_paper_decision(self, intent: "LeaderIntent") -> dict:
        """Construct the decision dict the PaperTrader expects.

        Mirrors the shape produced by :class:`ConfidenceEngine._emit` so
        the existing PaperTrader.open_trade gates (live_candidate,
        signal_audit, etc.) pass. The intent_id flows through
        ``trade_context`` so the decision_log → mempool_observations
        join is reconstructable end-to-end."""
        # Direction: 'yes' or 'no'. We don't know the polarity off the
        # intent alone — the upstream observer encodes it; mirror the
        # observer's default of 'yes' for unknowns so the paper trader
        # never veto's on a missing direction.
        return {
            "market_id": intent.market_id,
            "token_id": intent.token_id,
            "action": "follow",
            "direction": "yes",
            "size_usdc": float(intent.size_usdc),
            "confidence": 1.0,
            "leader_wallet": intent.wallet,
            "trade_context": {
                "source": "mempool_prefill_shadow",
                "intent_id": intent.intent_id,
                "tx_hash": intent.tx_hash,
                "expected_block": intent.expected_block,
                "live_candidate": True,
                "trade_age_s": 0.0,
            },
            "signal_audit": {
                "accepted": True,
                "source": "mempool_prefill",
                "intent_id": intent.intent_id,
            },
        }


def _extract_action(result: Any) -> str:
    """Best-effort pull of an action string off a confidence-engine
    response. Accepts a string, a dict with ``action``, an object with
    ``.action``, or ``None``."""
    if result is None:
        return "skip"
    if isinstance(result, str):
        return result.lower()
    action = getattr(result, "action", None)
    if action is None and isinstance(result, dict):
        action = result.get("action")
    if action is None:
        return "skip"
    return str(action).lower()


def _coerce_uuid(value: Any) -> uuid.UUID:
    """Coerce a stringy intent_id to a UUID. The migration column is
    UUID-typed; asyncpg accepts both ``uuid.UUID`` and the canonical
    string form, but coercing here gives a clear error on a malformed
    value rather than a cryptic asyncpg cast failure."""
    if isinstance(value, uuid.UUID):
        return value
    return uuid.UUID(str(value))
