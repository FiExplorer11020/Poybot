"""
Live-Match Detector — predicate that flags markets resolving in MINUTES
(active sports/eSports matches) so the bot does NOT FOLLOW leaders into
them.

Why this exists
---------------
The `MIN_HOURS_TO_RESOLUTION_FOLLOW=6h` gate is conceptually wrong on
live sport markets because `markets.end_date` is the dispute-window
expiration (often 7+ days AFTER the actual event), not the moment of
resolution. On 2026-05-17 the bot lost 9 trades at -96/98% by following
leaders into matches that resolved within MINUTES while the filter saw
~169h to end_date and waved them through.

Detection sources (in order of authority)
-----------------------------------------
1. **`markets.is_live_match=TRUE`** — populated by Agent A's hourly
   Gamma enrichment job. Authoritative when present. Reason code:
   ``gamma_flag``.
2. **Regex on `markets.question`** — case-insensitive patterns that
   match the live segment shorthand sports/eSports books use
   ("Map 1", "Half 1", "Quarter 3", "Set 2", "Game 4", "Round 5",
   "Period 2", "Inning 8", "Over/Under …"). Reason codes:
   ``regex_map`` / ``regex_period``.
3. **Date-in-question** — question mentions today's date ("May 17",
   "today", ISO 2026-05-17). Reason: ``regex_today``.
4. **Volume spike** — sports category AND `volume_24h > 50_000`.
   Live in-play markets concentrate volume in the 1-3h window the
   match is live. Reason: ``volume_spike``.

Reason codes (returned in the tuple's second element)
-----------------------------------------------------
- ``gamma_flag``     — `markets.is_live_match=TRUE` was the deciding signal.
- ``regex_map``      — eSports "Map N" pattern matched.
- ``regex_period``   — generic sport segment pattern matched (Half/Quarter/
                       Set/Game/Round/Period/Inning).
- ``regex_today``    — today's date / "today" mentioned in the question.
- ``volume_spike``   — sports category + volume_24h above the live threshold.
- ``no_match``       — none of the signals fired; market is NOT live.
- ``unknown_market`` — no DB row found AND no inline `market_row` provided.

Notes
-----
- This module is purely a PREDICATE. It never side-effects (no Redis
  writes, no decision_log inserts). Callers are responsible for the
  refusal accounting (paper_trader and confidence_engine both already
  have rejection counters / SKIP logging plumbed).
- The detector is intentionally conservative: when in doubt about a
  generic "vs" question that lacks any segment markers, we return
  False (no_match) so we don't block legitimate long-dated futures
  like "Who wins Champions League 2027?".
- Date-in-question matches today's date in three common spellings
  (ISO, "Month Day", and the literal word "today") so the IPL-style
  question "IPL: Punjab Kings vs Royal Challengers Bengaluru" still
  fires when paired with a sports volume spike (the regex_period
  patterns don't trigger on that bare team-vs-team form).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from loguru import logger

from src.config import settings
from src.database.connection import get_db

# ---------------------------------------------------------------------------
# Regex catalog                                                              #
# ---------------------------------------------------------------------------
# All patterns are compiled once and applied with re.IGNORECASE so a
# question's capitalisation doesn't affect the verdict.
#
# Each entry: (compiled_pattern, reason_code). The first match wins;
# more specific patterns (e.g. eSports "Map N") come before the
# generic sports patterns so the reason code stays meaningful.

_REGEX_MAP = re.compile(r"\bMap\s+\d+", re.IGNORECASE)

# Generic segment markers — we cluster these under regex_period so the
# operator's dashboard rejection tally stays readable. The names below
# are correct (Half/Quarter/Set/Game/Round/Period/Inning); the reason
# code itself is the cluster label.
_REGEX_SEGMENT_PATTERNS = [
    re.compile(r"\bHalf\s+\d+", re.IGNORECASE),
    re.compile(r"\bQuarter\s+\d+", re.IGNORECASE),
    re.compile(r"\bSet\s+\d+", re.IGNORECASE),
    re.compile(r"\bGame\s+\d+", re.IGNORECASE),
    re.compile(r"\bRound\s+\d+", re.IGNORECASE),
    re.compile(r"\bPeriod\s+\d+", re.IGNORECASE),
    re.compile(r"\bInning\s+\d+", re.IGNORECASE),
]

# Over/Under <numeric> is a live betting line. We require a number to
# avoid matching the phrase in long-dated futures ("Will Trump go
# over/under the line on …").
_REGEX_OVER_UNDER = re.compile(r"\bOver\s*/\s*Under\b[^\d\n]*\d", re.IGNORECASE)

# Today-in-question. Three spellings:
#   - literal word "today"
#   - ISO "YYYY-MM-DD"
#   - "Month Day" ("May 17", "May 17th", "May 17, 2026")
_REGEX_TODAY_WORD = re.compile(r"\btoday\b", re.IGNORECASE)
_MONTHS = (
    "January|February|March|April|May|June|July|August|"
    "September|October|November|December|"
    "Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec"
)


def _today_patterns(today: datetime | None = None) -> list[re.Pattern[str]]:
    """Build the date-of-today patterns at call time so a long-running
    process picks up date rollover (the patterns are cheap to compile)."""
    if today is None:
        today = datetime.now(tz=timezone.utc)
    iso = today.strftime("%Y-%m-%d")
    # Map month number to its full + short names so "May 17" or
    # "05-17" both match. Strftime gives us full names already.
    full_month = today.strftime("%B")
    short_month = today.strftime("%b")
    day = today.day
    # Day-with-suffix patterns ("17", "17th"). The trailing
    # `(?:st|nd|rd|th)?` is non-capturing + optional so plain "17"
    # works too.
    day_with_suffix = rf"{day}(?:st|nd|rd|th)?"
    patterns = [
        _REGEX_TODAY_WORD,
        re.compile(rf"\b{re.escape(iso)}\b"),
        re.compile(
            rf"\b(?:{full_month}|{short_month})\.?\s+{day_with_suffix}\b",
            re.IGNORECASE,
        ),
    ]
    return patterns


# ---------------------------------------------------------------------------
# Result dataclass + public API                                              #
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LiveMatchVerdict:
    """Structured result for callers that want more than the (bool, reason)
    tuple. The public `is_live_match` returns the tuple form to stay
    callable from the existing filter stack with no plumbing changes."""

    is_live: bool
    reason: str
    market_id: str
    question: str | None
    category: str | None
    volume_24h: float | None
    gamma_flag: bool | None


def _coerce_volume(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"true", "t", "1", "yes", "y"}
    return None


def _check_regex(question: str) -> str | None:
    """Return a reason code if any structural live-match regex matches.

    Order matters: the more specific eSports `Map N` pattern is tried
    first so its reason code (`regex_map`) is preserved when both it
    and a generic segment pattern would match (rare but possible).
    """
    if _REGEX_MAP.search(question):
        return "regex_map"
    for pattern in _REGEX_SEGMENT_PATTERNS:
        if pattern.search(question):
            return "regex_period"
    if _REGEX_OVER_UNDER.search(question):
        return "regex_period"
    return None


def _check_today_in_question(
    question: str, today: datetime | None = None
) -> bool:
    """True iff the question contains today's date in any common spelling."""
    for pattern in _today_patterns(today):
        if pattern.search(question):
            return True
    return False


def _check_volume_spike(
    category: str | None,
    volume_24h: float | None,
    threshold: float,
) -> bool:
    """Sports + high recent volume is the canonical live-event signature.

    We intentionally restrict the volume spike to `category='sports'`
    because crypto markets routinely sustain $50k+ rolling 24h volume
    without being "live" in the sport-match sense the filter targets.
    """
    if category is None or volume_24h is None:
        return False
    if str(category).strip().lower() != "sports":
        return False
    return volume_24h > threshold


async def _fetch_market_row(market_id: str) -> dict | None:
    """Best-effort DB lookup. Returns None when the row is missing OR
    the DB layer is unavailable — callers fall back to ``unknown_market``.

    The optional ``is_live_match`` column is selected with COALESCE so
    we don't crash on installations that haven't applied Agent A's
    migration yet (the column is allowed to be NULL).
    """
    try:
        async with get_db() as conn:
            row = await conn.fetchrow(
                """
                SELECT
                    market_id,
                    question,
                    category,
                    volume_24h,
                    is_live_match
                FROM markets
                WHERE market_id = $1
                """,
                market_id,
            )
    except Exception as exc:
        # The column may not exist on cold-start before Agent A's
        # migration runs — fall back to the legacy projection.
        if "is_live_match" in str(exc):
            try:
                async with get_db() as conn:
                    row = await conn.fetchrow(
                        """
                        SELECT
                            market_id,
                            question,
                            category,
                            volume_24h
                        FROM markets
                        WHERE market_id = $1
                        """,
                        market_id,
                    )
            except Exception as inner_exc:
                logger.debug(
                    f"live_match_detector: DB fallback failed for "
                    f"market={market_id}: {inner_exc}"
                )
                return None
        else:
            logger.debug(
                f"live_match_detector: DB lookup failed for "
                f"market={market_id}: {exc}"
            )
            return None
    if row is None:
        return None
    return dict(row)


async def _resolve_volume_threshold() -> float:
    """Best-effort read of the volume threshold from RuntimeConfig with
    a settings fallback. Never raises — a config glitch must not silently
    disable the live-match detector."""
    fallback = float(
        getattr(settings, "LIVE_MATCH_VOLUME_THRESHOLD", 50_000.0)
    )
    try:
        from src.control.runtime_config import get_runtime_config

        cfg = get_runtime_config()
        effective = await cfg.effective()
        value = effective.get("live_match_volume_threshold")
        if value is not None:
            return float(value)
    except Exception as exc:
        logger.debug(
            f"live_match_detector: runtime_config volume read failed: {exc}"
        )
    return fallback


async def _resolve_required_signals() -> int:
    """Plan 2026-05-19 P0-5 — runtime-tunable multi-signal requirement.

    Returns the minimum number of independent live-match signals that
    must fire concurrently before the detector returns True. Default 2
    (one signal alone — typically gamma_flag — is no longer sufficient).
    """
    fallback = int(getattr(settings, "LIVE_MATCH_REQUIRE_SIGNALS", 2))
    try:
        from src.control.runtime_config import get_runtime_config

        cfg = get_runtime_config()
        effective = await cfg.effective()
        value = effective.get("live_match_require_signals")
        if value is not None:
            return max(1, int(value))
    except Exception as exc:
        logger.debug(
            f"live_match_detector: runtime_config signal-count read failed: {exc}"
        )
    return max(1, fallback)


async def is_live_match(
    market_id: str,
    market_row: dict | None = None,
    *,
    now: datetime | None = None,
) -> tuple[bool, str]:
    """Predicate: True iff the market looks like a LIVE sport/eSports match.

    Plan 2026-05-19 P0-5 refactor: instead of returning on the first
    signal that fires (which made gamma_flag alone 24% of skip reasons
    in live diagnostic 2026-05-18), this predicate now collects ALL
    signals and returns True only when the count >= the runtime-tunable
    ``live_match_require_signals`` threshold (default 2). When a single
    signal fires but the threshold isn't met, the reason is suffixed
    with ``|signals=N/M`` so the dashboard can show the partial match.

    Reason code semantics:
      * ``no_match`` — zero signals fired.
      * ``<signal_code>|signals=1/N`` — single signal fired (below threshold).
      * ``<primary>+<secondary>|signals=K/N`` — K signals fired (above
        threshold). Primary = highest-authority signal that fired
        (gamma_flag > regex_map > regex_period > regex_today > volume_spike).
      * ``unknown_market`` — DB lookup failed.

    Parameters and return shape are otherwise unchanged for backward
    compatibility with the 18+ existing tests.
    """
    if not market_id:
        return False, "unknown_market"

    row = market_row if market_row is not None else await _fetch_market_row(market_id)
    if row is None:
        return False, "unknown_market"

    required = await _resolve_required_signals()

    # Backward compat: required==1 preserves the legacy short-circuit
    # semantics (return immediately on the first signal that fires
    # with its bare reason code). This keeps the 18+ legacy tests and
    # any dashboards that string-match the reason working unchanged.
    if required <= 1:
        return await _is_live_match_legacy(row, now=now)

    fired: list[str] = []

    # ---- Signal 1: authoritative Gamma flag (Agent A's enrichment). ----
    gamma_flag = _coerce_bool(row.get("is_live_match"))
    if gamma_flag is True:
        fired.append("gamma_flag")

    question = row.get("question") or ""
    if not isinstance(question, str):
        question = str(question)
    category = row.get("category")
    volume_24h = _coerce_volume(row.get("volume_24h"))

    # ---- Signal 2: regex on the question. ----
    regex_reason = _check_regex(question)
    if regex_reason is not None:
        fired.append(regex_reason)

    # ---- Signal 3: today-in-question. ----
    if _check_today_in_question(question, now):
        fired.append("regex_today")

    # ---- Signal 4: sports volume spike. ----
    threshold = await _resolve_volume_threshold()
    if _check_volume_spike(category, volume_24h, threshold):
        fired.append("volume_spike")

    if not fired:
        return False, "no_match"

    if len(fired) >= required:
        # Cluster reason: primary+secondary order preserved (gamma_flag
        # outranks regex_map outranks regex_period outranks regex_today
        # outranks volume_spike — fired list already follows this order).
        reason = "+".join(fired) + f"|signals={len(fired)}/{required}"
        return True, reason

    # Partial match — log the partial fire for dashboard visibility but
    # do NOT block. Caller treats this identically to ``no_match``.
    reason = fired[0] + f"|signals={len(fired)}/{required}"
    return False, reason


async def _is_live_match_legacy(
    row: dict, *, now: datetime | None
) -> tuple[bool, str]:
    """Legacy single-signal short-circuit kept for backward compat when
    the operator pins ``live_match_require_signals=1``."""
    gamma_flag = _coerce_bool(row.get("is_live_match"))
    if gamma_flag is True:
        return True, "gamma_flag"

    question = row.get("question") or ""
    if not isinstance(question, str):
        question = str(question)
    category = row.get("category")
    volume_24h = _coerce_volume(row.get("volume_24h"))

    regex_reason = _check_regex(question)
    if regex_reason is not None:
        return True, regex_reason

    if _check_today_in_question(question, now):
        return True, "regex_today"

    threshold = await _resolve_volume_threshold()
    if _check_volume_spike(category, volume_24h, threshold):
        return True, "volume_spike"

    return False, "no_match"


async def evaluate_live_match(
    market_id: str,
    market_row: dict | None = None,
    *,
    now: datetime | None = None,
) -> LiveMatchVerdict:
    """Structured variant of :func:`is_live_match` for callers that want
    the source data in the same call (used by the dashboard inspector
    to render the detector verdict alongside the underlying signals).
    """
    row = market_row if market_row is not None else await _fetch_market_row(market_id)
    if row is None:
        return LiveMatchVerdict(
            is_live=False,
            reason="unknown_market",
            market_id=market_id,
            question=None,
            category=None,
            volume_24h=None,
            gamma_flag=None,
        )
    is_live, reason = await is_live_match(market_id, row, now=now)
    return LiveMatchVerdict(
        is_live=is_live,
        reason=reason,
        market_id=market_id,
        question=row.get("question"),
        category=row.get("category"),
        volume_24h=_coerce_volume(row.get("volume_24h")),
        gamma_flag=_coerce_bool(row.get("is_live_match")),
    )


async def live_match_block_enabled() -> bool:
    """Operator gate. When False, the predicate still runs (so the
    dashboard shows what WOULD have been rejected) but callers should
    not refuse the trade. Defaults to True — the bug this filter exists
    to fix is severe."""
    fallback = bool(getattr(settings, "LIVE_MATCH_BLOCK_ENABLED", True))
    try:
        from src.control.runtime_config import get_runtime_config

        cfg = get_runtime_config()
        effective = await cfg.effective()
        value = effective.get("live_match_block_enabled")
        if value is None:
            return fallback
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            return value.strip().lower() in {"true", "1", "yes", "on"}
    except Exception as exc:
        logger.debug(
            f"live_match_detector: runtime_config enable read failed: {exc}"
        )
    return fallback
