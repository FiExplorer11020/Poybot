"""Consume ``mempool:leader_intent`` and route to fire or paper.

Round 7 / The Front Door — § 3.6 + § 3.7.

The IntentRouter is the second half of R7's hot path: it consumes
:class:`src.mempool.tx_decoder.LeaderIntent` events from the
``mempool:leader_intent`` Redis Stream (consumer group
``prefill_router``), applies a strict-order risk decision tree, and
either:

* fires a pre-signed order via :class:`PreSignedPool.fire`
  (LIVE mode, gated by both the global killswitch AND a R7-specific
  ``PREFILL_LIVE_ENABLED`` runtime config flag), OR
* opens a paper position via :class:`src.engine.paper_trader.PaperTrader.open_trade`
  (SHADOW mode — default for the first 30 days of operation).

Decision tree (in order, fail-fast)
-----------------------------------
1. **Killswitch (strict path)**. ``get_killswitch().is_real_execution_enabled(bypass_cache=True)``.
   Phase 0 R2 B mandates ``bypass_cache=True`` on the live execution
   path — between a DB flip and the 2s Redis cache rewrite, a
   fast-path read can see a stale ``True`` and leak real orders. We
   refuse on stale.

   The killswitch is consulted EVEN IN SHADOW MODE — a paper-trade
   shadow during a killswitch-off state is still consuming Falcon /
   Postgres budget on a wallet we're not supposed to trade. Refusing
   shadow on killswitch-off keeps the metrics clean.

2. **Confidence engine**: ``confidence_engine.recommend(intent.wallet, intent.market_id)``
   must return ``FOLLOW`` or ``"volume_anticipation"`` (the Round 9
   strategy mode). ``FADE`` / ``SKIP`` short-circuit out — we don't
   front-run a leader we'd FADE.

3. **Position size limit**: ``size_usdc <= current_capital × MAX_POSITION_PCT``.
   This is the same hard cap RiskManager enforces; we re-check here
   because the pre-fill path skips the post-decision RiskManager
   gate (RiskManager is part of the FOLLOW codepath, not the
   intent-router codepath).

4. **Cooldown**: ``risk_manager.in_cooldown(intent.wallet, intent.market_id)``
   must return ``False``. Re-uses the existing cooldown ledger so
   the prefill path doesn't reset cooldown bookkeeping the FOLLOW
   path maintains.

5. **Pool match**: ``pool.fire(intent)`` returns non-``None``.

Any check failure → log to ``decision_log`` with
``action='prefill_skip'`` + reason, increment
``polybot_intent_router_decisions_total{result=<reason>}``, return.

Success path
------------
SHADOW MODE (``PREFILL_LIVE_ENABLED=False``, default for 30 days):
    * ``await paper_trader.open_trade(synthetic_decision)`` where
      ``synthetic_decision`` is the same payload shape PaperTrader
      already accepts.
    * Log ``decision_log`` with ``action='prefill_shadow'``.
    * No CLOB submit.
    * Record ``mempool_observations`` row with
      ``fire_result='shadow'``.

LIVE MODE (``PREFILL_LIVE_ENABLED=True``, post-soak):
    * Consult killswitch AGAIN (TOCTOU defence — a kill could land
      between step 1 and pool.fire).
    * ``filled = await pool.fire(intent)``.
    * If ``filled is None``: ``decision_log`` row with
      ``action='prefill_pool_miss'``, return.
    * Log ``decision_log`` with ``action='prefill_intent'`` +
      ``intent_id``.
    * Publish to ``trades:stream`` (Round 6 cross-source reconciler
      will catch the source mismatch and reconcile against the
      eventual on-chain confirmation) with ``source='prefill'``.
    * Update ``mempool_observations`` row with ``fire_result='filled'``,
      ``fired_at``, ``latency_ms_to_fire``.

Architecture note
-----------------
The router does NOT call :meth:`src.engine.live_trader.LiveTrader.open_trade`
directly. The LiveTrader is a CONSUMER of the existing decisions
channel; the prefill path is a NEW caller of the underlying CLOB
submission primitive (the pool's :meth:`fire`). Both code paths
honour the same killswitch contract.

Wave-2 plumbing
---------------
The shadow-mode flag lives in :class:`src.control.runtime_config.RuntimeConfig`::

    enabled = await RuntimeConfig.get_value(
        "prefill_live_enabled", default=False
    )

The dashboard's Risk & Config cockpit can flip it at runtime without
a redeploy. Operators MUST validate the 30-day soak metrics before
flipping it.

See ``docs/ROUND_7_MEMPOOL_AND_PREFILL.md`` § 3.6 + § 3.7 for the
spec.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover — type-only imports
    from src.execution.prefill.pool import PreSignedPool
    from src.mempool.tx_decoder import LeaderIntent


_WAVE_2_REF = "Wave 2 — see docs/ROUND_7_MEMPOOL_AND_PREFILL.md § 3.6"


class IntentRouter:
    """Stream consumer + decision-tree gate for the prefill path.

    Owns:
      * a :class:`src.control.redis_streams.StreamConsumer` bound to
        ``mempool:leader_intent`` with group ``prefill_router``.
      * references to the pool / live_trader / paper_trader /
        confidence_engine / risk_manager / killswitch collaborators.
      * a background task ``pool.expire_stale`` loop (owned here
        because the pool has no asyncio lifecycle of its own).
    """

    def __init__(
        self,
        pool: "PreSignedPool",
        live_trader,
        paper_trader,
        confidence_engine,
        risk_manager,
        killswitch,
    ) -> None:
        """Bind to all collaborators.

        Parameters
        ----------
        pool
            :class:`src.execution.prefill.pool.PreSignedPool` instance.
        live_trader
            :class:`src.engine.live_trader.LiveTrader` — used as a
            type-stable reference for shared collaborators (the strict
            execution path itself goes through ``pool.fire``, NOT
            ``live_trader.open_trade``). Pass the engine's existing
            instance.
        paper_trader
            :class:`src.engine.paper_trader.PaperTrader` — the shadow
            sink during the 30-day soak.
        confidence_engine
            :class:`src.engine.confidence_engine.ConfidenceEngine`.
            Read-only; the router never updates the Thompson
            posteriors (those update on RESOLUTION via the existing
            close path).
        risk_manager
            :class:`src.engine.risk_manager.RiskManager`. Read-only;
            the router consults ``in_cooldown`` + position-size cap.
            The full RiskManager pipeline is NOT re-run — we already
            know the upstream pipeline approved this leader once
            (it's tier-0/1 in the wallet_universe).
        killswitch
            :class:`src.control.killswitch.KillswitchService` (the
            module singleton from
            :func:`src.control.killswitch.get_killswitch`).
        """
        raise NotImplementedError(_WAVE_2_REF)

    async def start(self) -> None:
        """Spin up the consumer + background tasks.

        Wave-2 implementation outline:
          1. Resolve the shadow-mode flag from runtime_config at boot
             (and re-resolve on every intent in :meth:`_on_intent`,
             since the operator may flip mid-soak).
          2. Build the :class:`StreamConsumer`. Register ``self._on_intent``
             as the handler. ``consumer.start()``.
          3. Spawn a periodic ``self._pool.expire_stale`` task.

        Idempotent.
        """
        raise NotImplementedError(_WAVE_2_REF)

    async def stop(self) -> None:
        """Cancel background tasks + close the consumer. Idempotent."""
        raise NotImplementedError(_WAVE_2_REF)

    async def _on_intent(self, intent: "LeaderIntent") -> None:
        """Apply the R7 § 3.6 decision tree to a single intent.

        Step-by-step (Wave-2 implementation):

        1. **Killswitch strict path**::

               try:
                   ok = await self._killswitch.is_real_execution_enabled(
                       bypass_cache=True
                   )
               except Exception:
                   ok = False
               if not ok:
                   self._record_skip(intent, "killswitch_off")
                   return

        2. **Confidence engine**::

               rec = await self._confidence_engine.recommend(
                   intent.wallet, intent.market_id
               )
               if rec.action not in ("follow", "volume_anticipation"):
                   self._record_skip(intent, f"confidence_{rec.action}")
                   return

        3. **Position size cap**::

               cap = current_capital * settings.MAX_POSITION_PCT
               if Decimal(intent.size_usdc) > Decimal(str(cap)):
                   self._record_skip(intent, "size_cap")
                   return

        4. **Cooldown**::

               if await self._risk_manager.in_cooldown(
                   intent.wallet, intent.market_id
               ):
                   self._record_skip(intent, "cooldown")
                   return

        5. **Pool match**: read ``PREFILL_LIVE_ENABLED`` from
           runtime_config.

           If ``False`` (SHADOW mode, default):
               Build a synthetic decision dict (same shape PaperTrader
               accepts on its existing decision channel) and call
               ``await self._paper_trader.open_trade(decision)``.
               Insert a ``mempool_observations`` row with
               ``fire_result='shadow'``.

           If ``True`` (LIVE mode):
               * Re-consult killswitch (TOCTOU defence).
               * ``filled = await self._pool.fire(intent)``.
               * On ``None``: ``self._record_skip(intent, "pool_miss")``.
                 Insert observation with ``fire_result='pool_miss'``.
               * On success: log decision_log with
                 ``action='prefill_intent'`` + ``intent_id``; publish
                 ``trades:stream`` entry with ``source='prefill'``;
                 update ``mempool_observations`` row with
                 ``fire_result='filled'`` + latency fields.

        6. **Metrics**: every branch increments
           ``polybot_intent_router_decisions_total{result=<branch>}``.
           Observe ``polybot_intent_router_latency_seconds`` from
           ``intent.intent_received_at`` → "fire complete" wall-clock.

        7. **Trace correlation**: every log line / DB write uses
           ``intent.intent_id`` as the trace id so a single
           pre-confirmation decision is followable end-to-end through
           the stream → router → paper/live → reconciler chain.
        """
        raise NotImplementedError(_WAVE_2_REF)
