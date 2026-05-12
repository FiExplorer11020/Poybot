"""Microstructure feature derivation — Round 11 (The Microscope) § 3.2.

Subscribes to the ``book:events:stream`` Redis Stream (produced by
:mod:`src.observer.clob_book_observer`), runs the four detectors
in real-time, and writes per-minute rollups to
``microstructure_features`` plus per-wallet 30-day signatures to
``wallet_microstructure_signature``.

Components:
  * :mod:`src.microstructure.derivers`       — IcebergDetector,
                                                SpoofDetector,
                                                OrderFlowImbalanceCalculator,
                                                PlaceToFillTimingTracker,
                                                CancelToFillRatioTracker.
  * :mod:`src.microstructure.rollup`         — MicrostructureRollup
                                                (per-minute table writes).
  * :mod:`src.microstructure.wallet_signature` — WalletSignatureBatch
                                                (nightly per-wallet rollup).
  * :mod:`src.microstructure.daemon`         — MicrostructureDaemon
                                                composing the pipeline.

See :doc:`docs/ROUND_11_CLOB_BOOK_MICROSTRUCTURE.md` for the full spec.
"""

from src.microstructure.derivers import (
    CancelToFillRatioTracker,
    IcebergDetector,
    MicrostructureFeatureDeriver,
    OrderFlowImbalanceCalculator,
    PlaceToFillTimingTracker,
    SpoofDetector,
)
from src.microstructure.rollup import MicrostructureRollup
from src.microstructure.wallet_signature import WalletSignatureBatch

__all__ = (
    "CancelToFillRatioTracker",
    "IcebergDetector",
    "MicrostructureFeatureDeriver",
    "MicrostructureRollup",
    "OrderFlowImbalanceCalculator",
    "PlaceToFillTimingTracker",
    "SpoofDetector",
    "WalletSignatureBatch",
)
