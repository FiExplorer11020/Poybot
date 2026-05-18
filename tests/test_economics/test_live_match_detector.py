"""
Regression tests for the Strategy Upgrade 2026-05-17 Tier 1 fix #2 + #3:
the live-match detector that blocks FOLLOW signals into sport markets
that resolve in MINUTES (the 9-trade -96/98% loss pattern from
2026-05-17 confirmed in `docs/autonomous_session_2026_05_17_strategy/
02_STRUCTURAL_FIX_PLAN.md`).

Each test pins one of the detector's four signal sources so a future
refactor can't silently drop one branch:
- gamma_flag       — authoritative `markets.is_live_match=TRUE`
- regex_map        — eSports "Map N" pattern
- regex_period     — generic sports segment (Half/Quarter/Set/...)
- regex_today      — today's date / "today" in question
- volume_spike     — sports category + volume_24h > threshold
- no_match         — none of the above (politics, elections, futures...)
- unknown_market   — no DB row + no inline market_row provided

The IPL case ("Indian Premier League: Punjab Kings vs Royal Challengers
Bengaluru") is tested in BOTH branches (with and without Agent A's
Gamma flag populated) so the fallback wiring is verifiable.

All DB lookups are stubbed via `_with_market_row` so the tests stay
hermetic. RuntimeConfig is bypassed by passing rows inline.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from src.economics.live_match_detector import (
    LiveMatchVerdict,
    evaluate_live_match,
    is_live_match,
    live_match_block_enabled,
)


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #


def _row(
    *,
    market_id: str = "mkt-1",
    question: str = "",
    category: str | None = "sports",
    volume_24h: float | None = 0.0,
    is_live_match_flag: bool | None = None,
) -> dict:
    """Build a market row dict matching `_fetch_market_row` output shape."""
    return {
        "market_id": market_id,
        "question": question,
        "category": category,
        "volume_24h": volume_24h,
        "is_live_match": is_live_match_flag,
    }


def _patch_db_row(row: dict | None):
    """Patch `live_match_detector.get_db` so fetchrow returns `row`."""

    @asynccontextmanager
    async def _ctx():
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(return_value=row)
        yield conn

    return patch("src.economics.live_match_detector.get_db", side_effect=_ctx)


@pytest.fixture(autouse=True)
def _stub_runtime_volume_threshold():
    """Default the RuntimeConfig probe to a stable threshold so we can
    assert against $50k without needing a live Redis. Individual tests
    override by re-patching `_resolve_volume_threshold` directly."""
    with patch(
        "src.economics.live_match_detector._resolve_volume_threshold",
        AsyncMock(return_value=50_000.0),
    ):
        yield


@pytest.fixture(autouse=True)
def _stub_runtime_required_signals():
    """Plan 2026-05-19 P0-5 — legacy single-signal-tests fixture.

    The detector's new default requires >=2 concurrent signals; this
    fixture pins the threshold to 1 so the original 18 legacy tests
    (which exercise one signal at a time) continue to assert the
    expected single-signal True/False outcomes. New multi-signal tests
    (TestMultiSignalThreshold below) override this fixture locally.
    """
    with patch(
        "src.economics.live_match_detector._resolve_required_signals",
        AsyncMock(return_value=1),
    ):
        yield


# --------------------------------------------------------------------------- #
# Signal #1 — Authoritative Gamma flag                                        #
# --------------------------------------------------------------------------- #


class TestGammaFlag:
    """Agent A's `markets.is_live_match` column is the authoritative
    source. When present and TRUE, no other signal needs to fire."""

    @pytest.mark.asyncio
    async def test_gamma_flag_true_short_circuits(self):
        """When `is_live_match=TRUE`, the detector returns immediately
        with reason `gamma_flag` regardless of question content."""
        row = _row(
            question="Will BTC be above 100k on Dec 31?",  # no regex match
            category="crypto",  # wrong category for spike
            volume_24h=0.0,
            is_live_match_flag=True,
        )
        is_live, reason = await is_live_match("mkt-1", row)
        assert is_live is True
        assert reason == "gamma_flag"

    @pytest.mark.asyncio
    async def test_gamma_flag_false_does_not_force_true(self):
        """`is_live_match=FALSE` doesn't override other signals — the
        detector still evaluates regex / volume. A false Gamma flag on
        a "Map 1" market still trips the regex branch."""
        row = _row(
            question="Counter-Strike Major: Liquid vs FaZe — Map 1 Winner",
            category="sports",
            volume_24h=10.0,
            is_live_match_flag=False,
        )
        is_live, reason = await is_live_match("mkt-1", row)
        assert is_live is True
        assert reason == "regex_map"

    @pytest.mark.asyncio
    async def test_gamma_flag_null_falls_through_to_regex(self):
        """The realistic cold-start case: Agent A's enrichment hasn't
        run yet so `is_live_match` is NULL. Detector must fall through
        to the regex branch."""
        row = _row(
            question="Lakers vs Celtics — Quarter 3 Leader",
            category="sports",
            volume_24h=0.0,
            is_live_match_flag=None,
        )
        is_live, reason = await is_live_match("mkt-1", row)
        assert is_live is True
        assert reason == "regex_period"


# --------------------------------------------------------------------------- #
# Signal #2 — Regex on the question                                            #
# --------------------------------------------------------------------------- #


class TestRegexPatterns:
    """Each segment pattern must trigger independently. Order matters
    for the reason code (Map N is the most specific → its own bucket)."""

    @pytest.mark.parametrize(
        "question",
        [
            "Map 1 Winner — Team A vs Team B",
            "Will Team A win Map 2?",
            "Cloud9 to take Map 3",
        ],
    )
    @pytest.mark.asyncio
    async def test_map_n_returns_regex_map(self, question):
        row = _row(question=question, is_live_match_flag=None)
        is_live, reason = await is_live_match("mkt-1", row)
        assert is_live is True
        assert reason == "regex_map"

    @pytest.mark.parametrize(
        "question",
        [
            "Half 1 over 2.5 goals?",
            "Quarter 4 leader",
            "Set 2 winner — Djokovic vs Alcaraz",
            "Game 5 over/under 9.5",
            "Round 8 KO?",
            "Period 2 leader",
            "Inning 7 score",
        ],
    )
    @pytest.mark.asyncio
    async def test_segment_patterns_return_regex_period(self, question):
        """All non-Map segment patterns share the regex_period bucket."""
        row = _row(question=question, is_live_match_flag=None)
        is_live, reason = await is_live_match("mkt-1", row)
        assert is_live is True
        assert reason == "regex_period"

    @pytest.mark.asyncio
    async def test_over_under_with_number_matches(self):
        """`Over/Under 9.5` is a live betting line."""
        row = _row(
            question="Lakers vs Warriors — Over/Under 220.5 points",
            is_live_match_flag=None,
        )
        is_live, reason = await is_live_match("mkt-1", row)
        assert is_live is True
        assert reason == "regex_period"


# --------------------------------------------------------------------------- #
# Signal #3 — Today-in-question                                                #
# --------------------------------------------------------------------------- #


class TestTodayInQuestion:
    """The question mentions today's date in any common spelling."""

    @pytest.mark.asyncio
    async def test_iso_date_today(self):
        today = datetime(2026, 5, 17, tzinfo=timezone.utc)
        row = _row(
            question="Trump executive order on 2026-05-17?",
            is_live_match_flag=None,
        )
        is_live, reason = await is_live_match("mkt-1", row, now=today)
        assert is_live is True
        assert reason == "regex_today"

    @pytest.mark.asyncio
    async def test_month_day_format(self):
        today = datetime(2026, 5, 17, tzinfo=timezone.utc)
        row = _row(
            question="Will the price drop by May 17?",
            is_live_match_flag=None,
        )
        is_live, reason = await is_live_match("mkt-1", row, now=today)
        assert is_live is True
        assert reason == "regex_today"

    @pytest.mark.asyncio
    async def test_literal_today_word(self):
        today = datetime(2026, 5, 17, tzinfo=timezone.utc)
        row = _row(
            question="Anything happen today in DC?",
            is_live_match_flag=None,
        )
        is_live, reason = await is_live_match("mkt-1", row, now=today)
        assert is_live is True
        assert reason == "regex_today"

    @pytest.mark.asyncio
    async def test_other_date_does_not_match(self):
        """A question mentioning a non-today date must not fire."""
        today = datetime(2026, 5, 17, tzinfo=timezone.utc)
        row = _row(
            question="Will the Fed cut rates by 2027-01-15?",
            category="macro",
            volume_24h=100.0,
            is_live_match_flag=None,
        )
        is_live, reason = await is_live_match("mkt-1", row, now=today)
        assert is_live is False
        assert reason == "no_match"


# --------------------------------------------------------------------------- #
# Signal #4 — Sports volume spike                                              #
# --------------------------------------------------------------------------- #


class TestVolumeSpike:
    """Sports category + 24h volume above the threshold = likely live."""

    @pytest.mark.asyncio
    async def test_high_volume_sports_triggers(self):
        row = _row(
            question="Punjab Kings vs Royal Challengers Bengaluru",
            category="sports",
            volume_24h=150_000.0,  # well above $50k threshold
            is_live_match_flag=None,
        )
        is_live, reason = await is_live_match("mkt-1", row)
        assert is_live is True
        assert reason == "volume_spike"

    @pytest.mark.asyncio
    async def test_low_volume_sports_no_match(self):
        """Sports category + volume BELOW threshold + no regex match
        → no_match (long-dated futures, e.g. championship-winner bets)."""
        row = _row(
            question="Who wins the Champions League 2027?",
            category="sports",
            volume_24h=5_000.0,  # well under $50k
            is_live_match_flag=None,
        )
        is_live, reason = await is_live_match("mkt-1", row)
        assert is_live is False
        assert reason == "no_match"

    @pytest.mark.asyncio
    async def test_high_volume_crypto_does_not_trigger(self):
        """Crypto routinely sustains $50k+ rolling 24h volume without
        being a `live match`. Volume spike is sports-only."""
        row = _row(
            question="Will BTC be above 100k on Dec 31?",
            category="crypto",
            volume_24h=2_000_000.0,
            is_live_match_flag=None,
        )
        is_live, reason = await is_live_match("mkt-1", row)
        assert is_live is False
        assert reason == "no_match"

    @pytest.mark.asyncio
    async def test_threshold_is_exclusive(self):
        """Exactly at the threshold is NOT a spike — the gate is strict `>`."""
        row = _row(
            question="Real Madrid vs Barcelona",
            category="sports",
            volume_24h=50_000.0,  # equal to threshold
            is_live_match_flag=None,
        )
        is_live, reason = await is_live_match("mkt-1", row)
        assert is_live is False
        assert reason == "no_match"


# --------------------------------------------------------------------------- #
# Negative cases — long-dated futures + elections                              #
# --------------------------------------------------------------------------- #


class TestNegativeCases:
    """The detector MUST be conservative — false positives block
    legitimate FOLLOW trades. The plan acceptance criteria explicitly
    call out the election case as a must-pass."""

    @pytest.mark.asyncio
    async def test_us_election_2028_not_live(self):
        """Long-dated US election market: no regex match, politics
        category (would be blocked elsewhere), no volume spike."""
        row = _row(
            question="US Presidential Election 2028 winner?",
            category="politics",
            volume_24h=200_000.0,  # high volume but not sports
            is_live_match_flag=None,
        )
        is_live, reason = await is_live_match("mkt-1", row)
        assert is_live is False
        assert reason == "no_match"

    @pytest.mark.asyncio
    async def test_generic_vs_question_not_live(self):
        """`Team A vs Team B` alone is NOT enough — only segmented
        questions trigger. This protects championship-winner bets."""
        row = _row(
            question="Real Madrid vs Barcelona — La Liga champions 2027?",
            category="sports",
            volume_24h=10_000.0,
            is_live_match_flag=None,
        )
        is_live, reason = await is_live_match("mkt-1", row)
        assert is_live is False
        assert reason == "no_match"

    @pytest.mark.asyncio
    async def test_macro_market_not_live(self):
        row = _row(
            question="Fed funds rate cut in Q3 2026?",
            category="macro",
            volume_24h=80_000.0,
            is_live_match_flag=None,
        )
        is_live, reason = await is_live_match("mkt-1", row)
        assert is_live is False
        assert reason == "no_match"


# --------------------------------------------------------------------------- #
# Unknown market path                                                         #
# --------------------------------------------------------------------------- #


class TestUnknownMarket:
    @pytest.mark.asyncio
    async def test_no_db_row_no_inline_returns_unknown(self):
        with _patch_db_row(None):
            is_live, reason = await is_live_match("mkt-missing")
        assert is_live is False
        assert reason == "unknown_market"

    @pytest.mark.asyncio
    async def test_empty_market_id_returns_unknown(self):
        is_live, reason = await is_live_match("")
        assert is_live is False
        assert reason == "unknown_market"


# --------------------------------------------------------------------------- #
# IPL test case (the load-bearing real-world example)                          #
# --------------------------------------------------------------------------- #


class TestIPLCase:
    """The 2026-05-17 paper trade #23 lost -98% on:
       'Indian Premier League: Punjab Kings vs Royal Challengers Bengaluru'

    The detector MUST flag this market in BOTH the Gamma-populated and
    cold-start branches. The plan's acceptance test (§5) requires it."""

    QUESTION = (
        "Indian Premier League: Punjab Kings vs Royal Challengers Bengaluru"
    )

    @pytest.mark.asyncio
    async def test_with_gamma_flag_populated(self):
        """Agent A's enrichment ran — `is_live_match=TRUE`. Detector
        returns immediately with `gamma_flag`."""
        row = _row(
            question=self.QUESTION,
            category="sports",
            volume_24h=300_000.0,
            is_live_match_flag=True,
        )
        is_live, reason = await is_live_match("ipl-mkt-23", row)
        assert is_live is True
        assert reason == "gamma_flag"

    @pytest.mark.asyncio
    async def test_without_gamma_flag_fallback_to_volume(self):
        """Cold-start: Agent A's job hasn't run. Regex doesn't match
        (no `Map 1` / `Half 2` markers on the bare team-vs-team form)
        but the sports + high-volume heuristic catches it."""
        row = _row(
            question=self.QUESTION,
            category="sports",
            volume_24h=300_000.0,
            is_live_match_flag=None,
        )
        is_live, reason = await is_live_match("ipl-mkt-23", row)
        assert is_live is True
        assert reason == "volume_spike"

    @pytest.mark.asyncio
    async def test_without_gamma_low_volume_no_match(self):
        """If the IPL match hadn't yet attracted live volume (e.g.
        pre-match opening), and Agent A's flag hadn't run, the
        detector returns no_match — this is the ONE residual failure
        mode the plan's structural fixes still need Agent A for."""
        row = _row(
            question=self.QUESTION,
            category="sports",
            volume_24h=2_000.0,
            is_live_match_flag=None,
        )
        is_live, reason = await is_live_match("ipl-mkt-23", row)
        # Documented residual gap — kept so the test surface tells the
        # operator if Agent A's enrichment is the load-bearing piece.
        assert is_live is False
        assert reason == "no_match"


# --------------------------------------------------------------------------- #
# Structured verdict                                                          #
# --------------------------------------------------------------------------- #


class TestEvaluateLiveMatch:
    @pytest.mark.asyncio
    async def test_verdict_carries_source_signals(self):
        row = _row(
            question="Map 1 winner",
            category="sports",
            volume_24h=75_000.0,
            is_live_match_flag=False,
        )
        verdict = await evaluate_live_match("mkt-1", row)
        assert isinstance(verdict, LiveMatchVerdict)
        assert verdict.is_live is True
        assert verdict.reason == "regex_map"
        assert verdict.question == "Map 1 winner"
        assert verdict.category == "sports"
        assert verdict.volume_24h == 75_000.0
        assert verdict.gamma_flag is False

    @pytest.mark.asyncio
    async def test_verdict_unknown_market(self):
        with _patch_db_row(None):
            verdict = await evaluate_live_match("mkt-missing")
        assert verdict.is_live is False
        assert verdict.reason == "unknown_market"
        assert verdict.question is None


# --------------------------------------------------------------------------- #
# Master gate                                                                 #
# --------------------------------------------------------------------------- #


class TestLiveMatchBlockEnabled:
    @pytest.mark.asyncio
    async def test_default_is_enabled(self):
        """Without an explicit override, the gate defaults to True —
        the bug this filter exists to fix is severe."""
        # Patch runtime_config to return no override → falls back to settings.
        with patch(
            "src.control.runtime_config.get_runtime_config"
        ) as mock_get:
            mock_cfg = AsyncMock()
            mock_cfg.effective = AsyncMock(return_value={})
            mock_get.return_value = mock_cfg
            assert await live_match_block_enabled() is True

    @pytest.mark.asyncio
    async def test_override_to_false_disables_gate(self):
        with patch(
            "src.control.runtime_config.get_runtime_config"
        ) as mock_get:
            mock_cfg = AsyncMock()
            mock_cfg.effective = AsyncMock(
                return_value={"live_match_block_enabled": False}
            )
            mock_get.return_value = mock_cfg
            assert await live_match_block_enabled() is False


# --------------------------------------------------------------------------- #
# Plan 2026-05-19 P0-5 — multi-signal threshold (NEW)                          #
# --------------------------------------------------------------------------- #


class TestMultiSignalThreshold:
    """The new default requires >=2 signals before blocking. These tests
    pin the runtime threshold to 2 (production default) and verify the
    new behaviour: gamma_flag alone, regex alone, volume_spike alone all
    return (False, partial). Two co-firing signals return (True, joined).
    """

    @pytest.fixture(autouse=True)
    def _require_two_signals(self):
        with patch(
            "src.economics.live_match_detector._resolve_required_signals",
            AsyncMock(return_value=2),
        ):
            yield

    @pytest.mark.asyncio
    async def test_gamma_flag_alone_does_not_block(self):
        """The 18/05 prod-diagnostic finding: gamma_flag was 24% of skip
        reasons. With threshold=2, gamma_flag alone no longer blocks
        and the reason is suffixed with `|signals=1/2` for visibility."""
        row = _row(
            question="Champions League final 2027 winner?",
            category="sports",
            volume_24h=10_000.0,
            is_live_match_flag=True,
        )
        is_live, reason = await is_live_match("mkt-multi-1", row)
        assert is_live is False
        assert reason == "gamma_flag|signals=1/2"

    @pytest.mark.asyncio
    async def test_gamma_flag_plus_volume_spike_blocks(self):
        """Two co-firing signals (the genuine live-match scenario)
        return True with both reasons joined."""
        row = _row(
            question="Real Madrid vs Liverpool",
            category="sports",
            volume_24h=200_000.0,  # volume_spike fires
            is_live_match_flag=True,    # gamma_flag fires
        )
        is_live, reason = await is_live_match("mkt-multi-2", row)
        assert is_live is True
        assert reason == "gamma_flag+volume_spike|signals=2/2"

    @pytest.mark.asyncio
    async def test_regex_map_plus_today_blocks(self):
        """Two non-gamma signals also block when both fire."""
        today = datetime(2026, 5, 19, tzinfo=timezone.utc)
        row = _row(
            question="Map 1 of finals on 2026-05-19",
            category="sports",
            volume_24h=5_000.0,
            is_live_match_flag=False,
        )
        is_live, reason = await is_live_match("mkt-multi-3", row, now=today)
        assert is_live is True
        assert "regex_map" in reason
        assert "regex_today" in reason
        assert "signals=2/2" in reason

    @pytest.mark.asyncio
    async def test_regex_alone_does_not_block(self):
        row = _row(
            question="Map 1 winner",
            category="sports",
            volume_24h=5_000.0,
            is_live_match_flag=None,
        )
        is_live, reason = await is_live_match("mkt-multi-4", row)
        assert is_live is False
        assert reason == "regex_map|signals=1/2"

    @pytest.mark.asyncio
    async def test_no_signals_returns_no_match(self):
        row = _row(
            question="Will the Fed cut rates by Q3 2027?",
            category="macro",
            volume_24h=80_000.0,
            is_live_match_flag=None,
        )
        is_live, reason = await is_live_match("mkt-multi-5", row)
        assert is_live is False
        assert reason == "no_match"
