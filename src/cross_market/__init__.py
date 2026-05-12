"""Round 12 (The Periphery) — cross-venue clients + identity resolution.

Public re-exports keep tests + callers from caring about internal layout.
"""

from src.cross_market.kalshi_client import KalshiClient
from src.cross_market.manifold_client import ManifoldClient
from src.cross_market.position_aggregator import CrossMarketPositionAggregator
from src.cross_market.predictit_client import PredictItClient
from src.cross_market.wallet_resolver import (
    ResolutionResult,
    ResolutionSource,
    WalletResolver,
)

__all__ = [
    "CrossMarketPositionAggregator",
    "KalshiClient",
    "ManifoldClient",
    "PredictItClient",
    "ResolutionResult",
    "ResolutionSource",
    "WalletResolver",
]
