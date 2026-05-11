"""Universal Wallet Crawler (Round 6 / The Spine § 3.4).

Maintains the ``wallet_universe`` table — every wallet that has ever
traded on Polymarket, with light-touch metadata and an adaptive depth
tier (see :mod:`src.crawler.depth_tiers`).

Module shape:
  * :mod:`src.crawler.universe`     — WalletUniverse: table maintenance,
    one-time historical backfill, ongoing INSERT-on-new-wallet.
  * :mod:`src.crawler.depth_tiers`  — AdaptiveDepth: nightly tier
    review (promotion / demotion).

Driven by ``polymarket-crawler.service`` (systemd; ``infra/systemd/``).
"""
