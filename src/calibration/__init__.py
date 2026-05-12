"""Round 13 (The Mirror) — Continuous calibration loop + auto-disable.

Spec: ``docs/ROUND_13_CALIBRATION_AND_RESEARCH.md``.

The Mirror closes the loop on R6-R12. Every decision the bot makes is
captured with each model's prediction (R8 strategy, R9 volume forecast,
R10 causal ATE). A nightly batch replays yesterday's decisions and
computes per-model calibration loss; the drift detector compares it
against a rolling baseline and, after 3 consecutive days of |z| > 2,
**auto-suppresses** the model's contribution to the decision flow.

Public surface re-exported here:

* :class:`DecisionPrediction`, :func:`record_decision_predictions`,
  :func:`fill_actual_outcomes` — atomic prediction logging hook.
* :class:`ModelLossAggregator` — nightly Brier / MAPE / log-loss / CI
  coverage computation.
* :class:`ModelDriftMonitor` — z-score + consecutive-day counter +
  Telegram-rate-limited alert pathway.
* :class:`ModelAutoDisabler`, :func:`get_auto_disabler` — self-
  suppression mechanism with the ``follow_confidence`` protection
  guard (spec § 3.4).
* :class:`CalibrationDaemon` — daemon entrypoint orchestrating the
  three above for the nightly batch.
"""

from src.calibration.auto_disable import (  # noqa: F401
    DisableState,
    ModelAutoDisabler,
    PROTECTED_FROM_AUTO_DISABLE,
    get_auto_disabler,
    init_auto_disabler,
)
from src.calibration.decision_replay import (  # noqa: F401
    DecisionPrediction,
    DecisionPredictionLogger,
    fill_actual_outcomes,
    fill_actual_outcomes_for_position,
    record_decision_predictions,
)
from src.calibration.drift_detector import (  # noqa: F401
    DriftAlert,
    DriftBaseline,
    ModelDriftMonitor,
)
from src.calibration.loss_aggregator import (  # noqa: F401
    LossRecord,
    ModelLossAggregator,
    compute_brier,
    compute_ci_coverage,
    compute_log_loss,
    compute_mape,
)

__all__ = [
    "CalibrationDaemon",
    "DecisionPrediction",
    "DecisionPredictionLogger",
    "DisableState",
    "DriftAlert",
    "DriftBaseline",
    "LossRecord",
    "ModelAutoDisabler",
    "ModelDriftMonitor",
    "ModelLossAggregator",
    "PROTECTED_FROM_AUTO_DISABLE",
    "compute_brier",
    "compute_ci_coverage",
    "compute_log_loss",
    "compute_mape",
    "fill_actual_outcomes",
    "fill_actual_outcomes_for_position",
    "get_auto_disabler",
    "init_auto_disabler",
    "record_decision_predictions",
]


def _import_daemon() -> object:
    """Lazy daemon import — keeps the package importable in
    environments where the runtime SQL bindings haven't been wired
    yet (tests, research notebooks)."""
    from src.calibration.daemon import CalibrationDaemon  # noqa: F401

    return CalibrationDaemon


def __getattr__(name: str):  # noqa: D401 — lazy attribute
    """Lazy class loader for :class:`CalibrationDaemon`."""
    if name == "CalibrationDaemon":
        return _import_daemon()
    raise AttributeError(f"module 'src.calibration' has no attribute {name!r}")
