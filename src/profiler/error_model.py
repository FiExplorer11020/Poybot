"""
Error Model — predicts P(leader loses this trade).
3-phase hierarchical model that upgrades as more data accumulates.

Phase 1 (0-99 resolved):  Beta-Binomial per market category
Phase 2 (100-499 resolved): Bayesian Ridge regression (sklearn proxy)
Phase 3 (500+ resolved):  LightGBM + Platt calibration

CUSUM drift detection downgrades the phase if the model's accuracy drops.
"""

import importlib.util
import json
import pickle
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import numpy as np
from loguru import logger

from src.config import settings
from src.database.connection import get_db
from src.economics.models import ECONOMIC_MODEL_VERSION
from src.economics.versioning import valid_position_filter, valid_profile_learning_filter
from src.profiler.behavior_profiler import (
    _compute_deviation_score,
    _compute_maturity,
    _compute_process_insights,
    _cyclical_time_features,
    _default_profile,
    _ensure_profile_schema,
    _get_category_accuracy,
    _hours_since_category_trade,
    _hours_since_position_loss,
    _update_accuracy,
    _update_decision_process,
    _update_dirichlet,
    _update_entry_patterns,
    _update_sizing,
)

# ─── CUSUM parameters ────────────────────────────────────────────────────────
CUSUM_BASELINE = 0.15
CUSUM_SLACK = 0.05
CUSUM_THRESHOLD = 2.0
PHASE2_LOOKBACK_DAYS = 90
PHASE2_MAX_SAMPLES = 1000
PHASE3_MAX_SAMPLES = 5000
V1_POSITION_SQL = valid_position_filter()
V1_PROFILE_TABLE_SQL = valid_profile_learning_filter("leader_profiles")


def _phase3_supported() -> bool:
    return importlib.util.find_spec("lightgbm") is not None


@dataclass
class ErrorPrediction:
    p_error: float  # 0.0 to 1.0
    confidence: float  # 0.0 to 1.0 (lower = more uncertain)
    phase: int  # 1, 2, or 3
    drift_alert: bool  # True if CUSUM fired


class ErrorModel:
    def __init__(self) -> None:
        # Per-wallet running CUSUM statistic S (in-memory; persisted to DB implicitly
        # via phase downgrade).
        self._cusum_state: dict[str, float] = {}

    # ─── Public API ──────────────────────────────────────────────────────────

    async def predict(self, wallet: str, trade_context: dict) -> ErrorPrediction:
        """
        Predict P(leader loses this trade) based on current phase and context.

        trade_context keys:
            category        str   — market category (e.g. "crypto", "politics")
            is_contrarian   bool  — whether the trade goes against recent price momentum
            deviation_score float — how much the trade deviates from leader's typical behaviour
            size_ratio      float — current size / typical EWMA size
            liquidity_score float — from Falcon Market Insights (agent 575)
        """
        phase, profile, model_blob = await self._load_state(wallet)
        drift_alert = self._cusum_state.get(wallet, 0.0) > CUSUM_THRESHOLD

        if phase == 1 or model_blob is None:
            p_error, confidence = self._predict_phase1(profile.get("accuracy", {}), trade_context)
            return ErrorPrediction(
                p_error=p_error, confidence=confidence, phase=1, drift_alert=drift_alert
            )

        try:
            model = pickle.loads(model_blob)
        except Exception as e:
            logger.warning(f"Cannot unpickle model for {wallet}: {e}")
            p_error, confidence = self._predict_phase1(profile.get("accuracy", {}), trade_context)
            return ErrorPrediction(
                p_error=p_error, confidence=confidence, phase=1, drift_alert=drift_alert
            )

        features = self._build_features(trade_context)

        if phase == 2:
            p_error = self._predict_phase2(model, features)
            confidence = 0.6
        else:
            p_error = self._predict_phase3(model, features)
            confidence = 0.85

        return ErrorPrediction(
            p_error=p_error, confidence=confidence, phase=phase, drift_alert=drift_alert
        )

    async def update(self, wallet: str, position_result: dict) -> None:
        """
        Called when a position closes with a known outcome.

        position_result keys:
            category     str   — market category
            pnl_usdc     float — realised PnL (negative = loss)
            trade_context dict — same structure as passed to predict()
        """
        phase, profile, model_blob = await self._load_state(wallet)
        positions_resolved = profile.get("accuracy", {}).get("resolved_count", 0)
        runtime = _ensure_runtime_state(profile)

        actual_loss = float(position_result.get("pnl_usdc", 0)) < 0

        # Update CUSUM when a model exists (phase 2+)
        if model_blob is not None:
            ctx = position_result.get("trade_context", {})
            pred = await self.predict(wallet, ctx)
            prediction_error = abs(pred.p_error - (1.0 if actual_loss else 0.0))
            s_prev = self._cusum_state.get(wallet, 0.0)
            s_new = max(0.0, s_prev + prediction_error - CUSUM_BASELINE - CUSUM_SLACK)
            self._cusum_state[wallet] = s_new
            runtime["cusum_state"] = round(s_new, 6)
            runtime["drift_alert"] = s_new > CUSUM_THRESHOLD
            runtime["last_prediction_error"] = round(prediction_error, 6)
            runtime["last_outcome_at"] = datetime.now(tz=timezone.utc).isoformat()

            if s_new > CUSUM_THRESHOLD and phase > 1:
                logger.warning(f"CUSUM drift detected for {wallet} — downgrading error model")
                await self._downgrade_phase(wallet, phase, profile=profile)
                return
        else:
            runtime.setdefault("cusum_state", 0.0)
            runtime.setdefault("drift_alert", False)
            runtime["last_outcome_at"] = datetime.now(tz=timezone.utc).isoformat()

        # Check for phase upgrade
        new_phase = self._determine_phase(positions_resolved)
        if new_phase > phase:
            await self._upgrade_phase(wallet, new_phase, profile)
        else:
            await self._retrain_if_needed(wallet, phase, profile)
            await self._save_runtime_profile(wallet, profile)

    # ─── Phase prediction helpers ─────────────────────────────────────────────

    def _predict_phase1(self, accuracy: dict, trade_context: dict) -> tuple[float, float]:
        """
        Beta-Binomial prediction per category.

        P(error | category) = β_b / (β_a + β_b)  — Beta posterior mean
        Uninformed prior: β_a = β_b = 1 (uniform / Laplace smoothing).
        """
        category = trade_context.get("category", "unknown")
        by_cat = accuracy.get("by_category", {})

        if category in by_cat:
            cat = by_cat[category]
            beta_a = float(cat.get("beta_a", 1.0))
            beta_b = float(cat.get("beta_b", 1.0))
        else:
            beta_a, beta_b = 1.0, 1.0  # Uniform prior

        p_error = beta_b / (beta_a + beta_b)

        # Bayesian variance of a Beta(a, b) distribution:
        # Var = (a·b) / ((a+b)² · (a+b+1))
        total = beta_a + beta_b
        variance = (beta_a * beta_b) / (total**2 * (total + 1))
        # Scale to [0.1, 1.0]: more data → lower variance → higher confidence
        confidence = max(0.1, 1.0 - variance * 10)

        return round(p_error, 4), round(confidence, 4)

    def _predict_phase2(self, model: Any, features: np.ndarray) -> float:
        """Bayesian Ridge regression prediction, clamped to [0, 1]."""
        try:
            pred = float(model.predict(features.reshape(1, -1))[0])
            return max(0.0, min(1.0, pred))
        except Exception as e:
            logger.warning(f"Phase 2 predict error: {e}")
            return 0.5

    def _predict_phase3(self, model: Any, features: np.ndarray) -> float:
        """LightGBM calibrated classifier prediction."""
        try:
            proba = model.predict_proba(features.reshape(1, -1))[0]
            return float(proba[1]) if len(proba) > 1 else float(proba[0])
        except Exception as e:
            logger.warning(f"Phase 3 predict error: {e}")
            return 0.5

    # ─── Feature engineering ──────────────────────────────────────────────────

    def _build_features(self, trade_context: dict) -> np.ndarray:
        """
        Convert a trade_context dict into a fixed-length feature vector.

        Feature layout (18 elements):
          [0] category_code    — hash(category) % 100 / 100.0
          [1] is_contrarian    — 0.0 or 1.0
          [2] deviation_score  — float in [0, 1]
          [3] size_ratio       — current size / typical EWMA size
          [4] liquidity_score  — from Falcon agent 575
          [5] process_score
          [6] flip_rate
          [7] scale_in_rate
          [8] hours_since_last_trade (normalized)
          [9] hours_since_category_last_trade (normalized)
          [10] hours_since_last_loss (normalized)
          [11] category_accuracy
          [12] profile_maturity
          [13] confirmed_followers (normalized)
          [14] hour_sin
          [15] hour_cos
          [16] dow_sin
          [17] dow_cos
        """
        category = trade_context.get("category", "unknown")
        cat_code = abs(hash(category)) % 100 / 100.0
        is_contrarian = float(bool(trade_context.get("is_contrarian", False)))
        deviation_score = float(trade_context.get("deviation_score", 0.0))
        size_ratio = min(4.0, max(0.0, float(trade_context.get("size_ratio", 1.0)))) / 4.0
        liquidity_score = float(trade_context.get("liquidity_score", 0.5))
        process_score = float(trade_context.get("process_score", 0.5))
        flip_rate = float(trade_context.get("flip_rate", 0.0))
        scale_in_rate = float(trade_context.get("scale_in_rate", 0.0))
        hours_since_last_trade = _normalize_hours(
            trade_context.get("hours_since_last_trade"),
            horizon_hours=168.0,
        )
        hours_since_category_last_trade = _normalize_hours(
            trade_context.get("hours_since_category_last_trade"),
            horizon_hours=336.0,
        )
        hours_since_last_loss = _normalize_hours(
            trade_context.get("hours_since_last_loss"),
            horizon_hours=336.0,
        )
        category_accuracy = float(trade_context.get("category_accuracy", 0.5))
        profile_maturity = float(trade_context.get("profile_maturity", 0.0))
        confirmed_followers = min(
            1.0,
            max(0.0, float(trade_context.get("confirmed_followers", 0.0))) / 10.0,
        )
        hour_sin = float(trade_context.get("hour_sin", 0.0))
        hour_cos = float(trade_context.get("hour_cos", 1.0))
        dow_sin = float(trade_context.get("dow_sin", 0.0))
        dow_cos = float(trade_context.get("dow_cos", 1.0))
        return np.array(
            [
                cat_code,
                is_contrarian,
                deviation_score,
                size_ratio,
                liquidity_score,
                process_score,
                flip_rate,
                scale_in_rate,
                hours_since_last_trade,
                hours_since_category_last_trade,
                hours_since_last_loss,
                category_accuracy,
                profile_maturity,
                confirmed_followers,
                hour_sin,
                hour_cos,
                dow_sin,
                dow_cos,
            ],
            dtype=np.float64,
        )

    # ─── Phase management ────────────────────────────────────────────────────

    def _determine_phase(self, positions_resolved: int) -> int:
        """Return the appropriate model phase given how many positions have resolved."""
        if positions_resolved >= settings.MIN_RESOLVED_FOR_ERROR_P3 and _phase3_supported():
            return 3
        if positions_resolved >= settings.MIN_RESOLVED_FOR_ERROR_P2:
            return 2
        return 1

    async def _upgrade_phase(self, wallet: str, new_phase: int, profile: dict) -> None:
        """Train a new model for the given phase and persist it."""
        logger.info(f"Upgrading error model for {wallet} to phase {new_phase}")

        training_data = await self._fetch_training_data(wallet, phase=new_phase)
        if training_data is None or len(training_data["X"]) < 10:
            logger.warning(f"Insufficient training data for {wallet} phase {new_phase} upgrade")
            return

        features = np.array(training_data["X"])
        y = np.array(training_data["y"])

        try:
            if new_phase == 2:
                from sklearn.linear_model import BayesianRidge  # type: ignore[import]

                model = BayesianRidge()
                model.fit(features, y.astype(float))
            else:
                if not _phase3_supported():
                    logger.warning(f"LightGBM unavailable locally; keeping {wallet} on phase 2")
                    await self._save_model(wallet, 2, None)
                    runtime = _ensure_runtime_state(profile)
                    runtime["last_fit_at"] = datetime.now(tz=timezone.utc).isoformat()
                    runtime["last_fit_phase"] = 2
                    runtime["training_samples"] = int(len(features))
                    runtime["phase3_blocked_reason"] = "lightgbm_not_installed"
                    await self._save_runtime_profile(wallet, profile)
                    return
                from lightgbm import LGBMClassifier  # type: ignore[import]
                from sklearn.calibration import CalibratedClassifierCV  # type: ignore[import]

                base = LGBMClassifier(n_estimators=50, max_depth=3, verbose=-1)
                base.fit(features, y)
                model = CalibratedClassifierCV(base, cv="prefit", method="sigmoid")
                model.fit(features, y)

            blob = pickle.dumps(model)
            await self._save_model(wallet, new_phase, blob)
            runtime = _ensure_runtime_state(profile)
            runtime["last_fit_at"] = datetime.now(tz=timezone.utc).isoformat()
            runtime["last_fit_phase"] = new_phase
            runtime["training_samples"] = int(len(features))
            runtime.setdefault("cusum_state", 0.0)
            runtime.setdefault("drift_alert", False)
            await self._save_runtime_profile(wallet, profile)
        except Exception as e:
            logger.error(f"Phase {new_phase} training failed for {wallet}: {e}")

    async def _retrain_if_needed(self, wallet: str, phase: int, profile: dict) -> None:
        """Time-gated retraining so phases 2/3 stay current without refitting constantly."""
        if phase <= 1:
            return

        runtime = _ensure_runtime_state(profile)
        last_fit_at = _parse_iso_dt(runtime.get("last_fit_at"))
        if last_fit_at is None:
            await self._upgrade_phase(wallet, phase, profile)
            return

        if datetime.now(tz=timezone.utc) - last_fit_at >= _phase_refit_interval(phase):
            await self._upgrade_phase(wallet, phase, profile)

    async def _downgrade_phase(
        self,
        wallet: str,
        current_phase: int,
        profile: dict | None = None,
    ) -> None:
        """
        CUSUM alarm: downgrade one phase and wipe the serialised model so the
        system falls back to Beta-Binomial (or re-trains from scratch).
        """
        new_phase = max(1, current_phase - 1)
        self._cusum_state[wallet] = 0.0
        await self._save_model(wallet, new_phase, None)
        if profile is None:
            _, profile, _ = await self._load_state(wallet)
        runtime = _ensure_runtime_state(profile)
        runtime["cusum_state"] = 0.0
        runtime["drift_alert"] = False
        runtime["last_downgraded_at"] = datetime.now(tz=timezone.utc).isoformat()
        runtime["last_fit_phase"] = new_phase
        await self._save_runtime_profile(wallet, profile)

    # ─── DB helpers ──────────────────────────────────────────────────────────

    async def _load_state(self, wallet: str) -> tuple[int, dict, bytes | None]:
        """Load (error_model_phase, profile_json, error_model_blob) from DB."""
        try:
            async with get_db() as conn:
                row = await conn.fetchrow(
                    f"""
                    SELECT error_model_phase, error_model_blob, profile_json
                    FROM leader_profiles
                    WHERE wallet_address = $1
                      AND {V1_PROFILE_TABLE_SQL}
                    """,
                    wallet,
                )
                if row:
                    phase = int(row["error_model_phase"] or 1)
                    blob = row["error_model_blob"]
                    raw_profile = row["profile_json"]
                    if isinstance(raw_profile, str):
                        profile = json.loads(raw_profile)
                    else:
                        profile = dict(raw_profile) if raw_profile else {}
                    runtime = _ensure_runtime_state(profile)
                    self._cusum_state.setdefault(
                        wallet,
                        float(runtime.get("cusum_state", 0.0) or 0.0),
                    )
                    return phase, profile, blob
        except Exception as e:
            logger.debug(f"Load state error for {wallet}: {e}")
        return 1, {}, None

    async def _save_model(self, wallet: str, phase: int, blob: bytes | None) -> None:
        """Persist phase and serialised model blob to DB."""
        try:
            async with get_db() as conn:
                await conn.execute(
                    """
                    UPDATE leader_profiles
                    SET error_model_phase = $2,
                        error_model_blob  = $3,
                        economic_model_version = $4,
                        last_updated      = NOW()
                    WHERE wallet_address = $1
                    """,
                    wallet,
                    phase,
                    blob,
                    ECONOMIC_MODEL_VERSION,
                )
        except Exception as e:
            logger.error(f"Save model error for {wallet}: {e}")

    async def _save_runtime_profile(self, wallet: str, profile: dict) -> None:
        try:
            async with get_db() as conn:
                await conn.execute(
                    """
                    UPDATE leader_profiles
                    SET profile_json = $2::jsonb,
                        economic_model_version = $3,
                        last_updated = NOW()
                    WHERE wallet_address = $1
                    """,
                    wallet,
                    json.dumps(profile),
                    ECONOMIC_MODEL_VERSION,
                )
        except Exception as e:
            logger.error(f"Save runtime profile error for {wallet}: {e}")

    async def _fetch_training_data(self, wallet: str, phase: int = 2) -> dict | None:
        """
        Fetch historical closed positions for model training.

        Returns {"X": list[list[float]], "y": list[int]} or None if no data.
        """
        lookback_cutoff = None
        if phase == 2:
            lookback_cutoff = datetime.now(tz=timezone.utc) - timedelta(days=PHASE2_LOOKBACK_DAYS)
        limit = PHASE3_MAX_SAMPLES if phase >= 3 else PHASE2_MAX_SAMPLES

        try:
            async with get_db() as conn:
                rows = await conn.fetch(
                    f"""
                    SELECT
                        pr.market_id,
                        pr.token_id,
                        pr.direction,
                        pr.open_time,
                        pr.close_time,
                        pr.entry_price,
                        pr.size_usdc,
                        pr.pnl_usdc,
                        COALESCE(m.category, 'unknown') AS category,
                        COALESCE(m.liquidity_score, 0.5) AS liquidity_score,
                        (
                            SELECT AVG(recent.price) FROM (
                                SELECT t.price
                                FROM trades_observed t
                                WHERE t.market_id = pr.market_id
                                  AND t.token_id = pr.token_id
                                  AND t.time < pr.open_time
                                ORDER BY t.time DESC
                                LIMIT 10
                            ) recent
                        ) AS avg_recent_price
                    FROM (
                        SELECT market_id, token_id, direction, open_time, close_time,
                               entry_price, size_usdc, pnl_usdc
                        FROM positions_reconstructed
                        WHERE wallet_address = $1
                          AND pnl_usdc IS NOT NULL
                          AND {V1_POSITION_SQL}
                          AND ($2::timestamptz IS NULL OR open_time >= $2)
                        ORDER BY open_time DESC
                        LIMIT {limit}
                    ) pr
                    LEFT JOIN markets m ON pr.market_id = m.market_id
                    ORDER BY pr.open_time ASC
                    """,
                    wallet,
                    lookback_cutoff,
                )

                if not rows:
                    return None

                earliest_open = rows[0]["open_time"]
                latest_open = rows[-1]["open_time"]
                trade_window_start = earliest_open - timedelta(days=30)

                observed_trades = await conn.fetch(
                    """
                    SELECT
                        t.market_id,
                        t.token_id,
                        t.side,
                        t.size_usdc,
                        t.time,
                        COALESCE(m.category, 'unknown') AS category
                    FROM trades_observed t
                    LEFT JOIN markets m ON m.market_id = t.market_id
                    WHERE t.wallet_address = $1
                      AND t.is_leader = TRUE
                      AND t.time >= $2
                      AND t.time <= $3
                    ORDER BY t.time ASC, t.id ASC
                    """,
                    wallet,
                    trade_window_start,
                    latest_open,
                )

                follower_edges = await conn.fetch(
                    """
                    SELECT first_observed
                    FROM follower_edges
                    WHERE leader_wallet = $1
                      AND co_occurrences >= 5
                      AND same_direction_rate >= 0.7
                    ORDER BY first_observed ASC
                    """,
                    wallet,
                )
        except Exception as e:
            logger.error(f"Fetch training data error: {e}")
            return None

        features, y = [], []
        rolling_profile = _default_profile()
        _ensure_profile_schema(rolling_profile)
        trade_index = 0
        edge_first_observed = [row["first_observed"] for row in follower_edges]

        for row in rows:
            open_time = row["open_time"]
            while (
                trade_index < len(observed_trades)
                and observed_trades[trade_index]["time"] is not None
                and observed_trades[trade_index]["time"] <= open_time
            ):
                trade_row = observed_trades[trade_index]
                _update_decision_process(
                    rolling_profile,
                    {
                        "market_id": trade_row.get("market_id", ""),
                        "side": trade_row.get("side", ""),
                        "size_usdc": float(trade_row.get("size_usdc") or 0.0),
                        "category": trade_row.get("category") or "unknown",
                        "time": trade_row["time"].isoformat(),
                    },
                )
                trade_index += 1

            category = row["category"] or "unknown"
            entry_price = float(row["entry_price"] or 0.5)
            avg_recent_price = (
                float(row["avg_recent_price"]) if row["avg_recent_price"] is not None else None
            )
            direction = row["direction"] or "yes"
            is_contrarian = False
            if avg_recent_price is not None:
                if direction == "yes":
                    is_contrarian = entry_price < avg_recent_price
                else:
                    is_contrarian = entry_price > avg_recent_price

            trade = {
                "market_id": row["market_id"],
                "side": "BUY",
                "size_usdc": float(row["size_usdc"] or 0.0),
                "category": category,
                "time": open_time.isoformat(),
                "is_contrarian": is_contrarian,
            }
            process_insights = _compute_process_insights(rolling_profile, trade)
            confirmed_followers = sum(
                1
                for first_observed in edge_first_observed
                if first_observed is not None and first_observed <= open_time
            )
            profile_maturity = _compute_maturity(
                int(rolling_profile["accuracy"].get("resolved_count", 0) or 0),
                confirmed_followers,
            )
            trade_context = {
                "category": category,
                "is_contrarian": is_contrarian,
                "deviation_score": _compute_deviation_score(rolling_profile, trade),
                "size_ratio": _size_ratio_from_profile(rolling_profile, trade["size_usdc"]),
                "liquidity_score": float(row["liquidity_score"] or 0.5),
                "process_score": process_insights["process_score"],
                "flip_rate": process_insights["flip_rate"],
                "scale_in_rate": process_insights["scale_in_rate"],
                "hours_since_last_trade": process_insights["hours_since_last_trade"],
                "hours_since_category_last_trade": _hours_since_category_trade(
                    rolling_profile,
                    category,
                    open_time.isoformat(),
                ),
                "hours_since_last_loss": _hours_since_position_loss(
                    rolling_profile,
                    open_time.isoformat(),
                ),
                "category_accuracy": _get_category_accuracy(rolling_profile, category),
                "profile_maturity": profile_maturity,
                "confirmed_followers": confirmed_followers,
                **_cyclical_time_features(open_time.isoformat()),
            }
            features.append(self._build_features(trade_context).tolist())
            loss = 1 if float(row["pnl_usdc"] or 0.0) < 0.0 else 0
            y.append(loss)

            _update_dirichlet(rolling_profile, category)
            if float(row["size_usdc"] or 0.0) > 0:
                _update_sizing(rolling_profile, float(row["size_usdc"] or 0.0))
            _update_entry_patterns(rolling_profile, is_contrarian)
            _update_accuracy(rolling_profile, category, win=(loss == 0))
            if loss:
                close_time = row["close_time"] or row["open_time"]
                rolling_profile.setdefault("loss_analysis", {})["last_position_loss_at"] = (
                    close_time.isoformat() if close_time is not None else open_time.isoformat()
                )

        return {"X": features, "y": y}


def _normalize_hours(value: Any, horizon_hours: float) -> float:
    if value is None:
        return 1.0
    try:
        numeric = max(0.0, float(value))
    except (TypeError, ValueError):
        return 1.0
    return min(1.0, numeric / horizon_hours)


def _size_ratio_from_profile(profile: dict, size_usdc: float) -> float:
    ewma_size = float(profile.get("sizing", {}).get("ewma_size", 0.0) or 0.0)
    if ewma_size <= 0 or size_usdc <= 0:
        return 1.0
    return size_usdc / ewma_size


def _ensure_runtime_state(profile: dict) -> dict:
    state = profile.setdefault("error_model_runtime", {})
    state.setdefault("cusum_state", 0.0)
    state.setdefault("drift_alert", False)
    return state


def _parse_iso_dt(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _phase_refit_interval(phase: int) -> timedelta:
    if phase >= 3:
        return timedelta(days=7)
    return timedelta(hours=24)
