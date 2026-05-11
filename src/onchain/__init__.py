"""Polymarket CLOB on-chain ingestion (Round 6 / The Spine § 3.3).

Direct subscription to Polymarket CTF Exchange contract events on
Polygon. Every fill, cancel, and match emits a LOG event with native
wallet attribution in the topics — no REST cross-reference needed.

Module shape:
  * :mod:`src.onchain.clob_abi`      — pinned contract ABI definitions.
  * :mod:`src.onchain.event_decoder` — per-event-type ABI decoders.
  * :mod:`src.onchain.clob_listener` — long-lived RPC subscription +
    Redis-stream publisher + trades_observed UPSERT.

The listener runs as its own systemd-supervised process
(``polymarket-onchain.service``, see ``infra/systemd/``) and publishes
to ``chain:trades:stream`` for downstream consumers.
"""
