"""Round 12 (The Periphery) — Tweet intent classifier.

Per spec § 3.2, the production-floor classifier is rule-based + simple
keyword + heuristic. We ship a real ``HeuristicTweetClassifier`` that
gives the daemon a useful default the moment the migrations land, plus a
``LoadableTweetClassifier`` shell that mmaps a serialized sklearn
pipeline IF the operator delivers one (label sprint per spec § 8 Phase
12.A is operator-deliverable). The trained model is intentionally NOT
in the dependency tree (no transformers/torch) — heuristic carries the
MVP until the operator's labelling sprint completes.

Output contract (every classifier must return the same dict shape so
the daemon can persist it without a switch):

    {
        "intent": "entry_signal" | "exit_signal" | "noise",
        "confidence": float in [0, 1],
        "parsed_market": str | None,
        "parsed_direction": "yes" | "no" | None,
    }

Latency budget: heuristic < 1 ms; trained-model loader < 100 ms per
spec § 3.2.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

from loguru import logger


# Defensive metric imports (same pattern as src/profiler/feature_store.py).
try:
    from src.monitoring.metrics import (  # type: ignore[attr-defined]
        social_classifier_latency_seconds,
    )
except Exception:  # pragma: no cover
    class _NoOp:
        def labels(self, *_a, **_kw):
            return self

        def observe(self, *_a, **_kw):
            return None

    social_classifier_latency_seconds = _NoOp()  # type: ignore[assignment]


class TweetIntent(str, Enum):
    """The three-class intent taxonomy (spec § 3.2)."""

    ENTRY_SIGNAL = "entry_signal"
    EXIT_SIGNAL = "exit_signal"
    NOISE = "noise"


@dataclass(frozen=True)
class TweetClassification:
    """Per-tweet classifier output. Frozen so the daemon can hash + dedup."""

    intent: TweetIntent
    confidence: float
    parsed_market: str | None
    parsed_direction: str | None  # 'yes' | 'no' | None


def classification_to_dict(c: TweetClassification) -> dict[str, Any]:
    """Serialise to the row-shape that the daemon writes to social_signals."""
    return {
        "intent": c.intent.value,
        "confidence": float(c.confidence),
        "parsed_market": c.parsed_market,
        "parsed_direction": c.parsed_direction,
    }


class TweetIntentClassifier(Protocol):
    """Interface contract — every classifier must implement ``classify``."""

    def classify(self, text: str) -> TweetClassification:  # pragma: no cover
        ...


# ---------------------------------------------------------------------------
# Heuristic classifier — the MVP / fallback per spec § 3.2.
# ---------------------------------------------------------------------------

# Keyword lists are intentionally conservative — biased toward NOISE so
# the daemon doesn't flood feature derivation with low-quality entry/exit
# signals before the operator's labelling sprint trains a real model.
# Spec § 7 acceptance: NLP validation accuracy ≥ 80 % on a 100-tweet
# held-out set; the heuristic floor is much lower than that, which is
# why the deriver weights signals by `intent_confidence` (per spec).
_ENTRY_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        # Direct entry verbs paired with side / size markers.
        r"\b(just\s+entered|just\s+bought|just\s+opened|going\s+long|going\s+short)\b",
        r"\b(loading\s+up|buying|adding|long(?:ing)?\s+(?:up\s+)?on)\b",
        r"\bopen(?:ed|ing)?\s+(?:a\s+)?(?:new\s+)?position\b",
        # YES / NO with size or $ sign — typical Polymarket tweet shape.
        r"\b(?:bought|got|grabbed)\s+\$?\d+(?:k|m)?\s+(?:of\s+)?(?:yes|no)\b",
        r"\b(?:yes|no)\s+at\s+\d+(?:\.\d+)?\s*¢?\b",
    )
)

_EXIT_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\b(took\s+profit|tp(?:'d)?|closed\s+(?:my\s+)?position|sold\s+(?:out|the)\s+(?:position|bag))\b",
        r"\b(exited|cashing\s+out|cashed\s+out|out\s+at|trim(?:med|ming)?)\b",
        r"\b(stop(?:ped)?\s+out|stopped\s+loss|cut\s+(?:my\s+)?losses)\b",
        r"\bsold\s+\$?\d+(?:k|m)?\s+(?:of\s+)?(?:yes|no)\b",
    )
)

# Market-URL pattern (the canonical Polymarket slug shape).
_MARKET_URL_RE = re.compile(
    r"polymarket\.com/(?:event|market)/([a-z0-9\-]+)",
    re.IGNORECASE,
)

# Cashtag / market mention fallback. Captures "$xyz-market" style refs.
_MARKET_CASHTAG_RE = re.compile(r"\$([a-zA-Z0-9_\-]{3,40})")

# Direction extraction. "yes at 0.42" / "long YES" / "short NO".
_YES_RE = re.compile(r"\b(yes|long\s+yes|buy(?:ing)?\s+yes)\b", re.IGNORECASE)
_NO_RE = re.compile(r"\b(no|long\s+no|short\s+yes|buy(?:ing)?\s+no)\b", re.IGNORECASE)


def _parse_market(text: str) -> str | None:
    """Extract a market slug if the tweet contains a polymarket.com URL,
    falling back to a $cashtag if not. Conservative — returns None on
    ambiguity rather than guessing.
    """
    m = _MARKET_URL_RE.search(text)
    if m:
        return m.group(1).lower()
    m = _MARKET_CASHTAG_RE.search(text)
    if m:
        return m.group(1).lower()
    return None


def _parse_direction(text: str) -> str | None:
    """Returns 'yes' / 'no' / None. Mutually-exclusive heuristic —
    when both patterns fire we return None (ambiguous)."""
    has_yes = bool(_YES_RE.search(text))
    has_no = bool(_NO_RE.search(text))
    if has_yes and not has_no:
        return "yes"
    if has_no and not has_yes:
        return "no"
    return None


class HeuristicTweetClassifier:
    """The production-floor classifier (spec § 3.2).

    Pure-regex; deterministic; ~50µs per call. Confidence values are
    calibrated by hand against the spec's heuristic baseline:

      * 0.85 — direct verb-pattern hit (e.g., "just entered YES at 0.42").
      * 0.65 — ambiguous hit (verb pattern but no direction/market).
      * 0.55 — single weak signal hit.
      * 0.90 — explicit noise (e.g., empty / one-word / pure-emoji).
      * 0.60 — default-to-noise everything else.

    The numbers are intentionally biased toward "noise" — over-classifying
    a tweet as an entry/exit signal silently corrupts the H. SOCIAL R8
    features. Under-classification is recoverable (operator's labelling
    sprint retrains the model upward).
    """

    name: str = "heuristic_v1"

    def classify(self, text: str) -> TweetClassification:
        t0 = time.perf_counter()
        try:
            if not text or not text.strip():
                return TweetClassification(
                    intent=TweetIntent.NOISE,
                    confidence=0.95,
                    parsed_market=None,
                    parsed_direction=None,
                )
            normalized = text.strip()
            # Very short messages are noise (a single emoji, a `gm`, etc.).
            if len(normalized) < 8:
                return TweetClassification(
                    intent=TweetIntent.NOISE,
                    confidence=0.90,
                    parsed_market=None,
                    parsed_direction=None,
                )
            entry_hit = any(p.search(normalized) for p in _ENTRY_PATTERNS)
            exit_hit = any(p.search(normalized) for p in _EXIT_PATTERNS)
            parsed_market = _parse_market(normalized)
            parsed_direction = _parse_direction(normalized)

            # Both fired — ambiguous, default to noise so the deriver
            # doesn't get a self-cancelling pair.
            if entry_hit and exit_hit:
                return TweetClassification(
                    intent=TweetIntent.NOISE,
                    confidence=0.60,
                    parsed_market=parsed_market,
                    parsed_direction=parsed_direction,
                )
            if entry_hit:
                conf = 0.85 if parsed_direction else 0.65
                return TweetClassification(
                    intent=TweetIntent.ENTRY_SIGNAL,
                    confidence=conf,
                    parsed_market=parsed_market,
                    parsed_direction=parsed_direction,
                )
            if exit_hit:
                conf = 0.85 if parsed_direction else 0.65
                return TweetClassification(
                    intent=TweetIntent.EXIT_SIGNAL,
                    confidence=conf,
                    parsed_market=parsed_market,
                    parsed_direction=parsed_direction,
                )
            return TweetClassification(
                intent=TweetIntent.NOISE,
                confidence=0.60,
                parsed_market=parsed_market,
                parsed_direction=parsed_direction,
            )
        finally:
            try:
                social_classifier_latency_seconds.observe(time.perf_counter() - t0)
            except Exception:  # pragma: no cover — metric is no-op-safe
                pass


# ---------------------------------------------------------------------------
# Loadable classifier — sklearn pipeline shell (operator-deliverable).
# ---------------------------------------------------------------------------


class LoadableTweetClassifier:
    """Shell that loads an operator-delivered sklearn pipeline if one
    exists at ``model_path``, else falls back to the heuristic per spec
    § 3.2's "trained-model-file-absent" case.

    Why a shell now: the labelling sprint is operator work (spec § 8
    Phase 12.A — 5 hrs focused). The code path is the contract; the
    artefact is operator-delivered. Tests verify both branches: present
    artefact loads + predicts, absent falls back gracefully.

    The serialised artefact is a pickle of a sklearn ``Pipeline`` where
    the final estimator implements ``predict_proba`` over the 3 classes
    of :class:`TweetIntent`. The labels MUST match the enum values
    exactly (case-sensitive) — the loader rejects classifiers with
    mismatched labels.
    """

    name: str = "loadable_v1"

    def __init__(
        self,
        model_path: str | Path | None = None,
        fallback: TweetIntentClassifier | None = None,
    ) -> None:
        self._fallback = fallback or HeuristicTweetClassifier()
        self._pipeline: Any | None = None
        if model_path:
            self._load(Path(model_path))

    def _load(self, path: Path) -> None:
        if not path.exists():
            logger.info(
                f"LoadableTweetClassifier: no model at {path}; "
                f"falling back to heuristic per spec § 3.2."
            )
            return
        try:
            import pickle  # local import keeps the cold path lean
            with path.open("rb") as fh:
                pipeline = pickle.load(fh)
            # Light validation — the daemon should never silently pick
            # up a model with the wrong label set.
            classes = getattr(pipeline, "classes_", None)
            if classes is not None:
                expected = {e.value for e in TweetIntent}
                got = {str(c) for c in classes}
                if not expected.issubset(got):
                    logger.warning(
                        f"LoadableTweetClassifier: classes={got} missing "
                        f"required labels {expected - got}; falling back."
                    )
                    return
            self._pipeline = pipeline
            logger.info(
                f"LoadableTweetClassifier: loaded pipeline from {path}"
            )
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning(
                f"LoadableTweetClassifier: failed to load {path}: {exc}; "
                f"falling back to heuristic."
            )
            self._pipeline = None

    def classify(self, text: str) -> TweetClassification:
        if self._pipeline is None:
            return self._fallback.classify(text)
        t0 = time.perf_counter()
        try:
            # The pipeline accepts a list; we feed [text] and pull row 0.
            try:
                proba_rows = self._pipeline.predict_proba([text])
            except Exception as exc:
                logger.debug(
                    f"LoadableTweetClassifier: predict_proba failed "
                    f"({exc}); falling back to heuristic."
                )
                return self._fallback.classify(text)
            row = proba_rows[0]
            classes = list(getattr(self._pipeline, "classes_", []))
            # argmax → label
            try:
                top_idx = max(range(len(row)), key=lambda i: float(row[i]))
            except (TypeError, ValueError):
                return self._fallback.classify(text)
            label = str(classes[top_idx]) if classes else TweetIntent.NOISE.value
            try:
                intent = TweetIntent(label)
            except ValueError:
                intent = TweetIntent.NOISE
            conf = float(row[top_idx])
            # Even with a trained model we still run the simple regex
            # market + direction parsers — they're orthogonal to the
            # intent class. Spec § 3.2's parsed_market / parsed_direction
            # is a separate, deterministic step.
            return TweetClassification(
                intent=intent,
                confidence=conf,
                parsed_market=_parse_market(text),
                parsed_direction=_parse_direction(text),
            )
        finally:
            try:
                social_classifier_latency_seconds.observe(time.perf_counter() - t0)
            except Exception:  # pragma: no cover
                pass


__all__ = [
    "HeuristicTweetClassifier",
    "LoadableTweetClassifier",
    "TweetClassification",
    "TweetIntent",
    "TweetIntentClassifier",
    "classification_to_dict",
]
