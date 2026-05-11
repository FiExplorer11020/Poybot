"""Pre-confirmation execution layer (Round 7 / The Front Door § 3.5-3.6).

Two collaborating components:

* :mod:`src.execution.prefill.pool`          — :class:`PreSignedPool`
  warehouses pre-signed CLOB orders so the hot-path fire skips the
  ~50 ms signing latency.

* :mod:`src.execution.prefill.intent_router` — :class:`IntentRouter`
  consumes ``mempool:leader_intent`` (Redis Stream), applies the
  R7 § 3.6 decision tree (killswitch / confidence / risk / cooldown /
  pool match), and either fires a pre-signed order or routes to the
  paper trader (shadow mode default).

The router lives on the SAME process as the engine (it's a new
caller of :meth:`src.engine.live_trader.LiveTrader.open_trade`, not
a replacement). The pool is in-memory; it's rebuilt on engine boot
from the active wallet_universe + top-N markets.

See ``docs/ROUND_7_MEMPOOL_AND_PREFILL.md`` § 3.5-3.7 for the spec.
"""
