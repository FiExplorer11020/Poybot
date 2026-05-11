"""Polymarket mempool watcher (Round 7 / The Front Door § 3.1-3.4).

Subscribes to Polygon's pending-transaction stream via Erigon's filtered
``eth_subscribe('newPendingTransactions', {fromAddress: [...]})``
extension, decodes Polymarket CTF Exchange calldata, and emits
:class:`LeaderIntent` events to ``mempool:leader_intent`` (Redis Stream).

Module shape:
  * :mod:`src.mempool.node_client`     — MempoolSubscription + NonceTracker
  * :mod:`src.mempool.tx_decoder`      — CLOBTxDecoder + LeaderIntent
  * :mod:`src.mempool.wallet_index`    — WatchedWalletIndex (bloom filter)
  * :mod:`src.mempool.event_emitter`   — LeaderIntentPublisher (Redis Stream)
  * :mod:`src.mempool.main`            — daemon entrypoint

The daemon runs under ``polymarket-mempool.service`` (300 MB budget,
see ``infra/systemd/``) and feeds the pre-fill order router that lives
in :mod:`src.execution.prefill`.

See ``docs/ROUND_7_MEMPOOL_AND_PREFILL.md`` for the full spec.
"""
