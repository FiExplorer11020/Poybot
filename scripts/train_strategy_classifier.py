"""Train the R8 strategy_classifier LightGBM model from strategy_labels.

Reads the latest label per wallet from ``strategy_labels`` (across all
labellers, picking the most recent labelled_at), builds the 42-dim
feature vector via :class:`LeaderFeatureExtractor`, fits the
:class:`StrategyClassifier` (LightGBM + isotonic Platt), and saves
the pickle to :data:`settings.STRATEGY_CLASSIFIER_MODEL_PATH`
(default ``models/strategy_classifier.pkl``).

Post-Sprint 4 / Phase 5 — first-ever LightGBM training pass. Before
this script the strategy_classifier daemon ran on the uniform-prior
dummy (1/9 for every class → 100% "directional" at confidence 0.1111
which is just argmax on equal probabilities → tie-breaker picks the
first class in the canonical order).

Idempotent: re-running re-fits from scratch on the latest labels and
atomically replaces the pickle. The daemon picks up the new model on
next refresh cycle (24h) OR on container restart.

Run example
-----------

.. code-block:: bash

    docker exec polymarket_strategy_classifier python /app/scripts/train_strategy_classifier.py
    # or with custom path
    docker exec polymarket_strategy_classifier python /app/scripts/train_strategy_classifier.py \\
        --out /app/models/strategy_classifier.pkl --min-labels 30
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

import numpy as np
from loguru import logger

# Make the project root importable when invoked as a bare script.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import settings  # noqa: E402
from src.database.connection import close_pool, initialize_pool  # noqa: E402
from src.strategy_classifier.features import LeaderFeatureExtractor  # noqa: E402
from src.strategy_classifier.labeling.label_store import (  # noqa: E402
    StrategyLabelStore,
)
from src.strategy_classifier.model import (  # noqa: E402
    STRATEGY_CLASSES,
    StrategyClassifier,
)


async def fetch_labels_and_features(
    min_labels: int,
) -> tuple[np.ndarray, list[str], list[str]]:
    """Build (X, y, wallets) from the latest label per wallet.

    Returns:
        X: (n_samples, n_features) numeric array, NaN-tolerant.
        y: list of strategy strings.
        wallets: parallel list of wallet addresses (for diagnostics).
    """
    store = StrategyLabelStore()
    label_rows = await store.get_labelled_set_for_training()
    if len(label_rows) < min_labels:
        raise RuntimeError(
            f"Not enough labels: got {len(label_rows)} < {min_labels} required. "
            "Run scripts/auto_label_strategies.py first."
        )

    logger.info(f"Loaded {len(label_rows)} labelled wallets")

    extractor = LeaderFeatureExtractor()
    X_rows: list[np.ndarray] = []
    y_list: list[str] = []
    wallets: list[str] = []
    n_extract_fail = 0
    for row in label_rows:
        wallet = row["wallet_address"]
        strategy = row["primary_strategy"]
        asof_ts = row["asof_ts"]  # set by StrategyLabelStore at window_end midnight UTC
        if strategy not in STRATEGY_CLASSES:
            logger.warning(f"Skipping {wallet}: unknown strategy {strategy!r}")
            continue
        try:
            fv = await extractor.extract(wallet, asof_ts)
        except Exception as exc:
            logger.warning(f"Skipping {wallet}: feature extraction failed: {exc}")
            n_extract_fail += 1
            continue
        # FeatureVector.values is the (FEATURE_COUNT,) numpy array.
        X_rows.append(np.asarray(fv.values, dtype=float))
        y_list.append(strategy)
        wallets.append(wallet)

    if not X_rows:
        raise RuntimeError("No usable feature rows after extraction.")

    X = np.vstack(X_rows)
    logger.info(
        f"Built training matrix: X.shape={X.shape}, "
        f"n_extract_fail={n_extract_fail}, classes={sorted(set(y_list))}"
    )
    return X, y_list, wallets


async def run(args: argparse.Namespace) -> int:
    db_url = os.environ.get("DATABASE_URL") or settings.DATABASE_URL
    await initialize_pool(
        dsn=db_url,
        min_size=settings.DB_POOL_MIN,
        max_size=settings.DB_POOL_MAX,
    )
    try:
        X, y, wallets = await fetch_labels_and_features(args.min_labels)

        # Class distribution diagnostic.
        from collections import Counter
        dist = Counter(y)
        logger.info(f"Class distribution: {dict(dist)}")

        if len(dist) < 2:
            logger.error(
                f"Only 1 distinct class in labels ({list(dist.keys())}). "
                "Cannot train multiclass model. Need at least 2 classes."
            )
            return 3

        clf = StrategyClassifier()
        clf.fit(X, y)

        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        clf.save(out_path)
        logger.info(
            f"DONE: model saved to {out_path} "
            f"(n_samples={X.shape[0]}, n_features={X.shape[1]}, "
            f"n_classes={len(dist)})"
        )
    finally:
        await close_pool()
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        default=os.environ.get(
            "STRATEGY_CLASSIFIER_MODEL_PATH",
            "models/strategy_classifier.pkl",
        ),
        help=(
            "Output path for the pickle. Default reads "
            "STRATEGY_CLASSIFIER_MODEL_PATH env (or settings.* fallback) "
            "→ 'models/strategy_classifier.pkl'. The daemon reads from "
            "the same path on startup."
        ),
    )
    parser.add_argument(
        "--min-labels",
        type=int,
        default=30,
        help=(
            "Minimum label count required to proceed. Guards against "
            "training on a near-empty set (overfit garanti). 30 is a "
            "soft floor — at <60 the isotonic calibration usually fails "
            "and we fall back to raw LightGBM probabilities."
        ),
    )
    args = parser.parse_args()
    sys.exit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
