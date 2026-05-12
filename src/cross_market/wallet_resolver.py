"""Round 12 — Cross-venue wallet resolution (spec § 4.2).

Maps a Polymarket wallet → set of venue addresses (Kalshi, Manifold,
PredictIt, X handle). Three sources:

  1. **Manual seed** — operator-curated; row gets
     ``resolution_source='manual'`` and confidence=1.0.
  2. **Profile link** — public profile link between addresses (e.g.,
     leader's X bio mentions their Manifold + Kalshi handles);
     ``resolution_source='profile_link'``, confidence=1.0.
  3. **Behavioral fingerprint** — automatic match via R8 strategy
     class + R11 microstructure signature; confidence is the matcher's
     raw score, often below the operator-confirmation threshold —
     these rows are written but flagged for manual review.

The resolver is intentionally manual-in-the-loop: auto-matches DO NOT
affect production decisions until the operator confirms by upgrading
``resolution_source`` to ``'manual'`` (or the confidence climbs above
``CROSS_MARKET_MIN_RESOLUTION_CONFIDENCE``).

The strategy-class + microstructure-signature lookups are injected so
tests can pass synthetic profiles without standing up the full R8 / R11
graph.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Awaitable, Callable

from loguru import logger

from src.config import settings
from src.database.connection import get_db

# Defensive metric imports.
try:
    from src.monitoring.metrics import (  # type: ignore[attr-defined]
        crossmarket_resolution_attempts_total,
        crossmarket_resolved_operators,
    )
except Exception:  # pragma: no cover
    class _NoOp:
        def labels(self, *_a, **_kw):
            return self

        def inc(self, *_a, **_kw):
            return None

        def set(self, *_a, **_kw):
            return None

    crossmarket_resolution_attempts_total = _NoOp()  # type: ignore[assignment]
    crossmarket_resolved_operators = _NoOp()  # type: ignore[assignment]


class ResolutionSource(str, Enum):
    MANUAL = "manual"
    PROFILE_LINK = "profile_link"
    FINGERPRINT = "fingerprint"


@dataclass
class ResolutionResult:
    """One resolution decision returned by the resolver."""

    polymarket_wallet: str | None
    kalshi_account: str | None
    manifold_handle: str | None
    predictit_account: str | None
    x_handle: str | None
    resolution_source: ResolutionSource
    confidence: float
    notes: str | None = None

    @property
    def is_pending_review(self) -> bool:
        """An auto-match (fingerprint) below the confirmation floor
        stays pending until the operator promotes it."""
        return (
            self.resolution_source is ResolutionSource.FINGERPRINT
            and self.confidence < settings.CROSS_MARKET_MIN_RESOLUTION_CONFIDENCE
        )


# Type aliases for the injected matcher callables — keeps the public
# constructor surface readable.
FetchSignatureFn = Callable[[str], Awaitable[dict[str, Any] | None]]
FetchKalshiCandidatesFn = Callable[[str], Awaitable[list[dict[str, Any]]]]


class WalletResolver:
    """Resolve Polymarket wallets → venue addresses.

    The resolver supports three input paths:

      * :meth:`seed_manual` — operator manually inserts a confirmed
        mapping.
      * :meth:`resolve_via_profile_link` — operator provides a public
        URL anchoring the mapping (e.g., X bio).
      * :meth:`resolve_via_fingerprint` — automatic; runs the R8 +
        R11 signature matcher.

    All three write to ``cross_market_operators``. Auto-matches below
    ``CROSS_MARKET_MIN_RESOLUTION_CONFIDENCE`` are flagged
    ``is_pending_review`` via the notes column.
    """

    def __init__(
        self,
        *,
        fetch_polymarket_signature: FetchSignatureFn | None = None,
        fetch_kalshi_candidates: FetchKalshiCandidatesFn | None = None,
    ) -> None:
        # Injected matcher dependencies — None means the fingerprint
        # path is unavailable in this process (e.g., bootstrap before
        # R11 lands signatures).
        self._fetch_pm_sig = fetch_polymarket_signature
        self._fetch_kalshi_candidates = fetch_kalshi_candidates

    async def seed_manual(
        self,
        *,
        polymarket_wallet: str | None = None,
        kalshi_account: str | None = None,
        manifold_handle: str | None = None,
        predictit_account: str | None = None,
        x_handle: str | None = None,
        notes: str | None = None,
    ) -> ResolutionResult:
        """Operator-curated seed. Always confidence=1.0."""
        result = ResolutionResult(
            polymarket_wallet=polymarket_wallet,
            kalshi_account=kalshi_account,
            manifold_handle=manifold_handle,
            predictit_account=predictit_account,
            x_handle=x_handle,
            resolution_source=ResolutionSource.MANUAL,
            confidence=1.0,
            notes=notes,
        )
        await self._persist(result)
        try:
            crossmarket_resolution_attempts_total.labels(
                source="manual", result="confirmed"
            ).inc()
        except Exception:  # pragma: no cover
            pass
        return result

    async def resolve_via_profile_link(
        self,
        *,
        polymarket_wallet: str | None = None,
        kalshi_account: str | None = None,
        manifold_handle: str | None = None,
        predictit_account: str | None = None,
        x_handle: str | None = None,
        notes: str | None = None,
    ) -> ResolutionResult:
        """Public-link-anchored resolution. Confidence=1.0 (operator
        verified the link)."""
        result = ResolutionResult(
            polymarket_wallet=polymarket_wallet,
            kalshi_account=kalshi_account,
            manifold_handle=manifold_handle,
            predictit_account=predictit_account,
            x_handle=x_handle,
            resolution_source=ResolutionSource.PROFILE_LINK,
            confidence=1.0,
            notes=notes,
        )
        await self._persist(result)
        try:
            crossmarket_resolution_attempts_total.labels(
                source="profile_link", result="confirmed"
            ).inc()
        except Exception:  # pragma: no cover
            pass
        return result

    async def resolve_via_fingerprint(
        self,
        polymarket_wallet: str,
        *,
        confirmation_threshold: float | None = None,
    ) -> ResolutionResult | None:
        """Automatic match: fetch the Polymarket wallet's R8 + R11
        signature, compare against candidate Kalshi accounts, return
        the highest-scoring match (or None if no candidate clears the
        floor).

        Auto-matches below the operator's confirmation threshold are
        STILL persisted (so the operator's review queue picks them up)
        but flagged ``is_pending_review``.
        """
        if (
            self._fetch_pm_sig is None
            or self._fetch_kalshi_candidates is None
        ):
            logger.debug(
                "WalletResolver: fingerprint path requires both "
                "fetch_polymarket_signature + fetch_kalshi_candidates."
            )
            try:
                crossmarket_resolution_attempts_total.labels(
                    source="fingerprint", result="error"
                ).inc()
            except Exception:  # pragma: no cover
                pass
            return None
        pm_sig = await self._fetch_pm_sig(polymarket_wallet)
        if not pm_sig:
            try:
                crossmarket_resolution_attempts_total.labels(
                    source="fingerprint", result="rejected"
                ).inc()
            except Exception:  # pragma: no cover
                pass
            return None
        candidates = await self._fetch_kalshi_candidates(polymarket_wallet)
        if not candidates:
            try:
                crossmarket_resolution_attempts_total.labels(
                    source="fingerprint", result="rejected"
                ).inc()
            except Exception:  # pragma: no cover
                pass
            return None
        # Score each candidate. Score model: cosine-like similarity over
        # a small set of orthogonal signals. Conservative defaults; the
        # operator's labelling sprint upgrades this with a real matcher.
        best: tuple[float, dict[str, Any]] | None = None
        for cand in candidates:
            score = self._score_match(pm_sig, cand)
            if best is None or score > best[0]:
                best = (score, cand)
        assert best is not None  # candidates non-empty above
        score, cand = best
        threshold = (
            confirmation_threshold
            if confirmation_threshold is not None
            else settings.CROSS_MARKET_MIN_RESOLUTION_CONFIDENCE
        )
        result_label = (
            "confirmed" if score >= threshold else "pending_review"
        )
        notes = (
            f"fingerprint match score={score:.4f}; pending review"
            if score < threshold
            else f"fingerprint match score={score:.4f}; auto-confirmed"
        )
        result = ResolutionResult(
            polymarket_wallet=polymarket_wallet,
            kalshi_account=str(cand.get("account") or "") or None,
            manifold_handle=cand.get("manifold_handle"),
            predictit_account=cand.get("predictit_account"),
            x_handle=cand.get("x_handle"),
            resolution_source=ResolutionSource.FINGERPRINT,
            confidence=float(score),
            notes=notes,
        )
        await self._persist(result)
        try:
            crossmarket_resolution_attempts_total.labels(
                source="fingerprint", result=result_label
            ).inc()
        except Exception:  # pragma: no cover
            pass
        return result

    @staticmethod
    def _score_match(
        pm_sig: dict[str, Any], candidate: dict[str, Any]
    ) -> float:
        """Very small heuristic scorer: agreement on strategy class
        contributes 0.5, microstructure signature within 25% gives 0.3,
        active-hours overlap gives up to 0.2. Total ∈ [0, 1].

        This is intentionally simple — spec § 4.2 calls out "we don't
        take false-positive risk", so the score floor is operator-tuned
        via :data:`settings.CROSS_MARKET_MIN_RESOLUTION_CONFIDENCE`.
        """
        score = 0.0
        # Strategy-class agreement.
        pm_class = pm_sig.get("strategy_class")
        cand_class = candidate.get("strategy_class")
        if pm_class and cand_class and pm_class == cand_class:
            score += 0.5
        # Microstructure cancel-to-fill ratio agreement (within 25%).
        pm_c2f = pm_sig.get("cancel_to_fill_ratio_30d")
        cand_c2f = candidate.get("cancel_to_fill_ratio_30d")
        try:
            if pm_c2f is not None and cand_c2f is not None:
                a = float(pm_c2f)
                b = float(cand_c2f)
                if max(a, b) > 0 and abs(a - b) / max(a, b) <= 0.25:
                    score += 0.3
        except (TypeError, ValueError):
            pass
        # Active-hours bucket overlap.
        pm_hours = set(pm_sig.get("active_hours_utc") or [])
        cand_hours = set(candidate.get("active_hours_utc") or [])
        if pm_hours and cand_hours:
            overlap = len(pm_hours & cand_hours) / max(1, len(pm_hours | cand_hours))
            score += 0.2 * overlap
        return min(1.0, max(0.0, score))

    async def _persist(self, result: ResolutionResult) -> None:
        """Insert one ``cross_market_operators`` row. We don't dedupe at
        write time — duplicate writes are a feature (operator history)
        and the read-side surfaces the latest matching row."""
        try:
            async with get_db() as conn:
                await conn.execute(
                    """
                    INSERT INTO cross_market_operators (
                        polymarket_wallet, kalshi_account, manifold_handle,
                        predictit_account, x_handle,
                        resolution_source, confidence, resolved_at, notes
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                    """,
                    result.polymarket_wallet,
                    result.kalshi_account,
                    result.manifold_handle,
                    result.predictit_account,
                    result.x_handle,
                    result.resolution_source.value,
                    float(result.confidence),
                    datetime.now(tz=timezone.utc),
                    result.notes,
                )
        except Exception as exc:
            logger.warning(
                f"WalletResolver: persist failed for "
                f"pm={result.polymarket_wallet}: {exc}"
            )


__all__ = [
    "FetchKalshiCandidatesFn",
    "FetchSignatureFn",
    "ResolutionResult",
    "ResolutionSource",
    "WalletResolver",
]
