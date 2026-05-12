"""
HawkesCouplingDriftDetector — Round 9 (The Web).

Audit reference: docs/ROUND_9_MULTIVARIATE_HAWKES.md § 3.5.

When a leader's multivariate Hawkes BIC test starts REJECTING the
coupled model after previously ACCEPTING it, that's strong evidence
the leader's influence structure has changed — maybe their strategy
shifted (R8 drift), maybe the follower pool dispersed, maybe a
competing leader stole the followers. Either way, the volume forecast
is no longer valid → gate volume_anticipation entries until the next
nightly refit clarifies whether the change is transient or permanent.

The detector is **stateless** in itself — it queries
``multivariate_hawkes_fits`` and compares the latest fit's convergence
field with the second-latest. Drift = (prev == 'converged' AND latest
== 'bic_rejected'). A single drift transition fires a Prometheus
metric + returns ``drift_detected=True`` to callers.

Why we don't store a separate "is_in_drift" flag: the fit table is
already an append-only timeline. Querying the latest two rows is O(1)
with the (leader_wallet, fit_at DESC) index.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from loguru import logger

from src.database.connection import get_db


@dataclass
class DriftReport:
    """Output of ``evaluate``.

    ``drift_detected`` is True iff the leader just transitioned from
    'converged' to 'bic_rejected' between the two latest fits.
    """

    leader_wallet: str
    drift_detected: bool
    previous_convergence: Optional[str]
    latest_convergence: Optional[str]
    n_fits_seen: int


class HawkesCouplingDriftDetector:
    """Detect leaders whose multivariate Hawkes coupling has decayed.

    The detector emits a metric on every detected transition. Callers
    (decision_router, dashboard) consult ``evaluate`` for a single
    leader before sizing a volume_anticipation entry; if drift is
    flagged, the entry is suppressed.
    """

    def __init__(self) -> None:
        self._metric = self._resolve_metric()

    @staticmethod
    def _resolve_metric():
        """Best-effort resolve of the drift-transition counter. Falls
        back to a no-op when prometheus_client is unavailable."""
        try:  # pragma: no cover — exercised in production
            from src.monitoring.metrics import mvhawkes_couplings_accepted
            return mvhawkes_couplings_accepted
        except Exception:  # pragma: no cover
            class _NoOp:
                def labels(self, *_a, **_kw):
                    return self

                def inc(self, *_a, **_kw):
                    return None

                def dec(self, *_a, **_kw):
                    return None

            return _NoOp()

    async def evaluate(self, leader_wallet: str) -> DriftReport:
        """Look at the latest two fits and decide whether drift fired.

        Returns a DriftReport. If fewer than two fits exist for the
        leader, ``drift_detected`` is False (no transition can be
        measured yet).
        """
        try:
            async with get_db() as conn:
                rows = await conn.fetch(
                    """
                    SELECT convergence, fit_at
                    FROM multivariate_hawkes_fits
                    WHERE leader_wallet = $1
                    ORDER BY fit_at DESC
                    LIMIT 2
                    """,
                    leader_wallet,
                )
        except Exception as exc:
            logger.warning(
                f"HawkesCouplingDriftDetector: query failed for "
                f"{leader_wallet}: {exc}"
            )
            return DriftReport(
                leader_wallet=leader_wallet,
                drift_detected=False,
                previous_convergence=None,
                latest_convergence=None,
                n_fits_seen=0,
            )

        n = len(rows)
        if n < 2:
            return DriftReport(
                leader_wallet=leader_wallet,
                drift_detected=False,
                previous_convergence=(rows[0]["convergence"] if n else None),
                latest_convergence=(rows[0]["convergence"] if n else None),
                n_fits_seen=n,
            )

        latest = rows[0]["convergence"]
        prev = rows[1]["convergence"]
        drift = (prev == "converged" and latest == "bic_rejected")
        if drift:
            try:
                self._metric.labels(leader_wallet=leader_wallet).dec()
            except Exception:  # pragma: no cover
                pass
            logger.warning(
                f"HawkesCouplingDriftDetector: leader={leader_wallet[:10]} "
                f"drift {prev} → {latest}"
            )

        return DriftReport(
            leader_wallet=leader_wallet,
            drift_detected=drift,
            previous_convergence=prev,
            latest_convergence=latest,
            n_fits_seen=n,
        )


__all__ = ["HawkesCouplingDriftDetector", "DriftReport"]
