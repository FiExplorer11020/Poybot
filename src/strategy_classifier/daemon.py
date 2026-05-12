"""Strategy classifier daemon — periodic classification of tier-0/1 wallets.

Round 8 (The Lens) — daemon entrypoint for the
``polymarket-strategy-classifier.service`` systemd unit.

Loop:

    every STRATEGY_CLASSIFIER_REFRESH_INTERVAL_H hours:
        wallets <- tier-0 + tier-1 from wallet_universe
        for each wallet:
            asof_ts <- wallet.last_active
            features <- LeaderFeatureExtractor.extract(wallet, asof_ts)
            prediction <- StrategyClassifier.predict_one(features.values)
            drift_report <- StrategyDriftDetector.evaluate(wallet, prediction.strategy_probs)
            insert into leader_strategy_history
            update leaders.classification_json.strategy_fingerprint
            increment metrics

When ``STRATEGY_CONDITIONAL_CONFIDENCE_ENABLED=False`` (default), the
daemon still runs and writes history rows — that's the shadow phase
documented in spec § 7.D. The confidence engine just ignores the
``strategy_fingerprint`` until the operator flips the flag.

The classifier model itself is loaded lazily from a configurable path
on disk (default ``models/strategy_classifier.pkl``). If the file
doesn't exist, the daemon loads the uniform-prior dummy and logs a
warning — that lets the systemd unit come up cleanly before the
operator has run the training notebook.
"""
from __future__ import annotations

import asyncio
import json
import signal
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import redis.asyncio as redis_async
from loguru import logger

from src.config import settings
from src.database.connection import close_pool, get_db, initialize_pool
from src.logging_setup import configure_logging
from src.strategy_classifier.drift import StrategyDriftDetector
from src.strategy_classifier.features import (
    FEATURE_COUNT,
    LeaderFeatureExtractor,
)
from src.strategy_classifier.model import (
    STRATEGY_CLASSES,
    StrategyClassifier,
)

# Try to import the metric handles; fall back to no-ops in stripped envs.
try:  # pragma: no cover — exercised in the daemon path
    from src.monitoring.metrics import (
        classifier_calibration_loss,
        classifier_confidence,
        classifier_drift_score,
        classifier_feature_extraction_seconds,
        classifier_inference_seconds,
        classifier_loss,
        classifier_predictions_total,
        strategy_drift_detected_total,
        strategy_label_set_size,
        unsupervised_clusters_unmatched,
    )
except Exception:  # pragma: no cover — early-import fallback

    class _NoOpLabel:
        def labels(self, *_a, **_kw):
            return self

        def inc(self, *_a, **_kw):
            return None

        def set(self, *_a, **_kw):
            return None

        def observe(self, *_a, **_kw):
            return None

    classifier_predictions_total = _NoOpLabel()  # type: ignore[assignment]
    classifier_confidence = _NoOpLabel()  # type: ignore[assignment]
    classifier_loss = _NoOpLabel()  # type: ignore[assignment]
    classifier_calibration_loss = _NoOpLabel()  # type: ignore[assignment]
    classifier_drift_score = _NoOpLabel()  # type: ignore[assignment]
    strategy_drift_detected_total = _NoOpLabel()  # type: ignore[assignment]
    strategy_label_set_size = _NoOpLabel()  # type: ignore[assignment]
    unsupervised_clusters_unmatched = _NoOpLabel()  # type: ignore[assignment]
    classifier_inference_seconds = _NoOpLabel()  # type: ignore[assignment]
    classifier_feature_extraction_seconds = _NoOpLabel()  # type: ignore[assignment]


DEFAULT_MODEL_PATH = "models/strategy_classifier.pkl"


class StrategyClassifierDaemon:
    """Long-running classification loop. Shape mirrors
    :mod:`src.registry.refresher_main` for systemd consistency.
    """

    def __init__(
        self,
        model_path: str | Path | None = None,
        refresh_interval_h: float | None = None,
        drift_threshold: float | None = None,
        feature_extractor: LeaderFeatureExtractor | None = None,
        classifier: StrategyClassifier | None = None,
        drift_detector: StrategyDriftDetector | None = None,
    ) -> None:
        self._model_path = Path(
            model_path
            or getattr(settings, "STRATEGY_CLASSIFIER_MODEL_PATH", DEFAULT_MODEL_PATH)
        )
        self._refresh_s = float(
            (refresh_interval_h
             if refresh_interval_h is not None
             else getattr(settings, "STRATEGY_CLASSIFIER_REFRESH_INTERVAL_H", 24))
        ) * 3600.0
        self._drift_threshold = float(
            drift_threshold
            if drift_threshold is not None
            else getattr(settings, "STRATEGY_DRIFT_JS_THRESHOLD", 0.3)
        )
        self._features = feature_extractor or LeaderFeatureExtractor()
        self._classifier = classifier or self._load_classifier()
        self._drift = drift_detector or StrategyDriftDetector(
            threshold=self._drift_threshold
        )

        self._stop_event = asyncio.Event()
        self._running = False

    # ------------------------------------------------------------------ #
    # Lifecycle                                                          #
    # ------------------------------------------------------------------ #

    async def start(self) -> None:
        """Run the classification loop until ``stop()`` is called."""
        self._running = True
        self._stop_event.clear()
        logger.info(
            f"StrategyClassifierDaemon starting: model_path={self._model_path} "
            f"refresh_s={self._refresh_s} drift_threshold={self._drift_threshold}"
        )
        # Run one pass immediately, then sleep on the interval.
        while self._running and not self._stop_event.is_set():
            try:
                await self.run_one_pass()
            except asyncio.CancelledError:
                break
            except Exception as exc:  # pragma: no cover — top-level
                logger.exception(
                    f"StrategyClassifierDaemon: pass failed: {exc}"
                )
            # Cancellable sleep.
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self._refresh_s)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

    async def stop(self) -> None:
        """Gracefully end the loop. Idempotent."""
        self._running = False
        self._stop_event.set()
        logger.info("StrategyClassifierDaemon: stop signalled")

    # ------------------------------------------------------------------ #
    # One pass                                                           #
    # ------------------------------------------------------------------ #

    async def run_one_pass(self) -> dict[str, Any]:
        """Classify every tier-0 / tier-1 wallet once. Returns a small
        summary dict the operator can grep in journalctl.
        """
        wallets = await self._load_classifier_wallets()
        if not wallets:
            logger.info("StrategyClassifierDaemon: no wallets to classify this pass")
            return {"classified": 0, "drift_alerts": 0}

        classified = 0
        drift_alerts = 0
        for wallet_address, last_active in wallets:
            try:
                ok = await self._classify_one(wallet_address, last_active)
                if ok is not None:
                    classified += 1
                    if ok.get("drift_detected"):
                        drift_alerts += 1
            except Exception as exc:
                logger.warning(
                    f"StrategyClassifierDaemon: classify failed for "
                    f"wallet={wallet_address}: {exc}"
                )

        logger.info(
            f"StrategyClassifierDaemon pass complete: "
            f"classified={classified} drift_alerts={drift_alerts}"
        )
        return {"classified": classified, "drift_alerts": drift_alerts}

    async def _classify_one(
        self,
        wallet_address: str,
        last_active: datetime,
    ) -> dict[str, Any] | None:
        """Classify a single wallet end-to-end."""
        # 1. Feature extraction
        import time
        t0 = time.perf_counter()
        fv = await self._features.extract(wallet_address, last_active)
        t1 = time.perf_counter()
        try:
            classifier_feature_extraction_seconds.observe(t1 - t0)
        except Exception:
            pass
        if fv.values.shape[0] != FEATURE_COUNT:
            logger.warning(
                f"StrategyClassifierDaemon: feature vector for {wallet_address} "
                f"has wrong shape ({fv.values.shape!r}); skipping"
            )
            return None

        # 2. Inference
        prediction = self._classifier.predict_one(fv.values)
        t2 = time.perf_counter()
        try:
            classifier_inference_seconds.observe(t2 - t1)
            classifier_predictions_total.labels(
                strategy=prediction.primary_strategy, source="scheduled"
            ).inc()
            classifier_confidence.labels(
                strategy=prediction.primary_strategy
            ).observe(prediction.confidence)
        except Exception:
            pass

        # 3. Drift
        drift_report = await self._drift.evaluate(
            wallet_address,
            prediction.strategy_probs,
            classified_at=datetime.now(tz=timezone.utc),
        )
        try:
            classifier_drift_score.labels(wallet=wallet_address).set(
                drift_report.js_divergence
            )
            if drift_report.drift_detected:
                strategy_drift_detected_total.labels(
                    **{
                        "from": drift_report.primary_strategy_baseline or "unknown",
                        "to": prediction.primary_strategy,
                    }
                ).inc()
        except Exception:
            pass

        # 4. Persist
        await self._persist_history(
            wallet_address, prediction, fv.asof_ts, drift_report
        )
        await self._update_leader_fingerprint(wallet_address, prediction, drift_report.drift_detected)

        return {
            "primary_strategy": prediction.primary_strategy,
            "confidence": prediction.confidence,
            "drift_detected": drift_report.drift_detected,
            "js_divergence": drift_report.js_divergence,
        }

    # ------------------------------------------------------------------ #
    # DB I/O                                                             #
    # ------------------------------------------------------------------ #

    async def _load_classifier_wallets(self) -> list[tuple[str, datetime]]:
        """Tier 0 + tier 1 from ``wallet_universe`` — the wallets the spec
        actually classifies. Tier 2 stays in the universe but isn't
        classified (spec § 8).
        """
        try:
            async with get_db() as conn:
                rows = await conn.fetch(
                    """
                    SELECT wallet_address, last_active
                    FROM wallet_universe
                    WHERE depth_tier IN (0, 1)
                      AND last_active IS NOT NULL
                    ORDER BY total_volume_usdc_ever DESC
                    """
                )
            return [(r["wallet_address"], r["last_active"]) for r in rows]
        except Exception as exc:
            logger.warning(
                f"StrategyClassifierDaemon: _load_classifier_wallets failed: {exc}"
            )
            return []

    async def _persist_history(
        self,
        wallet_address: str,
        prediction: Any,
        asof_ts: datetime,
        drift_report: Any,
    ) -> None:
        try:
            async with get_db() as conn:
                await conn.execute(
                    """
                    INSERT INTO leader_strategy_history
                        (wallet_address, classified_at, primary_strategy,
                         confidence, strategy_probs, model_version, asof_ts,
                         drift_js_divergence, drift_detected)
                    VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7, $8, $9)
                    """,
                    wallet_address,
                    datetime.now(tz=timezone.utc),
                    prediction.primary_strategy,
                    float(prediction.confidence),
                    json.dumps(prediction.strategy_probs),
                    prediction.model_version,
                    asof_ts,
                    (
                        float(drift_report.js_divergence)
                        if drift_report.js_divergence is not None
                        else None
                    ),
                    bool(drift_report.drift_detected),
                )
        except Exception as exc:
            logger.warning(
                f"StrategyClassifierDaemon: history insert failed for "
                f"wallet={wallet_address}: {exc}"
            )

    async def _update_leader_fingerprint(
        self,
        wallet_address: str,
        prediction: Any,
        drift_detected: bool,
    ) -> None:
        """Merge ``strategy_fingerprint`` into ``leaders.classification_json``
        (migration 027 schema). Preserves existing keys.
        """
        patch = self._classifier.build_classification_json_patch(
            prediction, drift_detected=drift_detected
        )
        try:
            async with get_db() as conn:
                await conn.execute(
                    """
                    UPDATE leaders
                    SET classification_json =
                        COALESCE(classification_json, '{}'::jsonb)
                        || jsonb_build_object('strategy_fingerprint', $2::jsonb)
                    WHERE wallet_address = $1
                    """,
                    wallet_address,
                    json.dumps(patch),
                )
        except Exception as exc:
            logger.debug(
                f"StrategyClassifierDaemon: leaders fingerprint update failed "
                f"for wallet={wallet_address}: {exc}"
            )

    # ------------------------------------------------------------------ #
    # Bootstrap                                                          #
    # ------------------------------------------------------------------ #

    def _load_classifier(self) -> StrategyClassifier:
        if self._model_path.exists():
            try:
                clf = StrategyClassifier.load(self._model_path)
                logger.info(
                    f"StrategyClassifierDaemon: loaded model from {self._model_path}"
                )
                return clf
            except Exception as exc:
                logger.warning(
                    f"StrategyClassifierDaemon: failed to load model from "
                    f"{self._model_path}: {exc}. Falling back to uniform-prior dummy."
                )
        else:
            logger.warning(
                f"StrategyClassifierDaemon: no trained model at {self._model_path}. "
                "Daemon will use uniform-prior dummy (all 9 classes equally "
                "likely). Train via the labelling notebook before flipping "
                "strategy_conditional_confidence_enabled=true."
            )
        return StrategyClassifier()


# --------------------------------------------------------------------------- #
# Module-level entrypoint used by ``python -m src.strategy_classifier``       #
# (see __main__.py).                                                          #
# --------------------------------------------------------------------------- #


async def main() -> None:
    """Daemon body. Mirrors src.registry.refresher_main.main()."""
    level = configure_logging()
    logger.info(f"Starting StrategyClassifier daemon (log_level={level})")

    await initialize_pool(
        dsn=settings.DATABASE_URL,
        min_size=settings.DB_POOL_MIN,
        max_size=settings.DB_POOL_MAX,
    )
    redis_client = redis_async.from_url(settings.REDIS_URL, decode_responses=True)

    daemon = StrategyClassifierDaemon()

    stop_event = asyncio.Event()

    def _handle_signal(*_args: object) -> None:
        logger.info("StrategyClassifier daemon: shutdown signal received")
        stop_event.set()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _handle_signal)
        except NotImplementedError:  # pragma: no cover — Windows quirks
            pass

    run_task = asyncio.create_task(daemon.start())
    stop_waiter = asyncio.create_task(stop_event.wait())

    try:
        done, pending = await asyncio.wait(
            {run_task, stop_waiter}, return_when=asyncio.FIRST_COMPLETED
        )
        if stop_waiter in done:
            await daemon.stop()
        for t in pending:
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
    finally:
        try:
            await redis_client.aclose()
        except Exception:  # pragma: no cover
            logger.exception("StrategyClassifier daemon: redis aclose() raised")
        await close_pool()
        logger.info("StrategyClassifier daemon: stopped")


if __name__ == "__main__":  # pragma: no cover — module run path
    asyncio.run(main())
