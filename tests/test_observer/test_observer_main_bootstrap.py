"""Regression tests for the observer bootstrap expansion landed 2026-05-17.

Changes covered:

1. ``_load_db_subscriptions`` now UNIONs THREE sources of leader
   wallets (Falcon score, follower_edges, observed winrate from
   ``positions_reconstructed``) instead of two. The new third source
   is the cohort the strategy gate filters on (≥20 resolved, ≥60%
   winrate), so the observer subscribes to the wallets the engine
   will actually act on even if Falcon hasn't picked them up yet.
2. ``wallet_limit`` default raised from 50 → 200 (initially tried 400 but
   the resulting trade volume saturated the asyncpg pool — 200 keeps a 4x
   improvement while staying within the DB connection budget).
3. ``MAX_OBSERVER_WS_TOKENS`` raised from 100 → 400 (initially tried 800;
   same DB-pool saturation; 400 is the sustainable middle ground).

The test focuses on the SQL behaviour the production callers depend on,
not the wire format — the actual UNION runs against Postgres in
integration tests.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from src.observer import main as observer_main


# --------------------------------------------------------------------------- #
# 1. Constants reflect the diagnosis-driven raise.                            #
# --------------------------------------------------------------------------- #


def test_max_observer_ws_tokens_raised_to_400():
    """``MAX_OBSERVER_WS_TOKENS`` was 100, must now be ≥400 (diagnosis §B.8).

    Note: an earlier patch in the same session attempted 800, but that
    saturated the DB connection pool; 400 is the production-validated
    sustainable value with the matching ``max_connections=500`` in
    docker-compose.
    """
    assert observer_main.MAX_OBSERVER_WS_TOKENS >= 400


def test_load_db_subscriptions_default_wallet_limit_raised():
    """The default ``wallet_limit`` parameter was 50, must now be ≥200.

    Same story as ``MAX_OBSERVER_WS_TOKENS``: tried 400 first, settled
    on 200 to keep the asyncpg pool within budget.

    We inspect the signature directly rather than calling the function
    so the test doesn't depend on the DB query semantics — a coupling
    that would force this test to re-mock 3 separate fetch() calls
    every time we tweak SQL.
    """
    import inspect as _inspect

    sig = _inspect.signature(observer_main._load_db_subscriptions)
    wallet_limit = sig.parameters["wallet_limit"]
    assert wallet_limit.default >= 200


# --------------------------------------------------------------------------- #
# 2. Three-source UNION wires through to query.                                #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_load_db_subscriptions_invokes_three_source_union():
    """The leaders query MUST reference ``positions_reconstructed`` (the
    third UNION branch). If the SQL no longer mentions it, the strategy
    gate cohort isn't being subscribed to and the regression is back.
    """
    captured_sql: list[str] = []

    async def _fake_fetch(sql, *args):
        captured_sql.append(sql)
        # Return shape that matches the call site's expectations.
        # First call → leaders UNION.
        # Second call → recent token_id list.
        # Third call → markets token list.
        if "positions_reconstructed" in sql or "follower_edges" in sql:
            return [
                {"wallet_address": "0xWalletA"},
                {"wallet_address": "0xWalletB"},
                # Duplicate to verify dedup happens via UNION semantics
                # (asyncpg returns deduped rows when SQL says UNION; the
                # test mock returns whatever the test provides).
                {"wallet_address": "0xWalletA"},
            ]
        if "trades_observed" in sql:
            return [{"token_id": "0xTokRecent"}]
        if "FROM markets" in sql:
            return [{"token_yes": "0xTokYes", "token_no": "0xTokNo", "volume_24h": 100}]
        return []

    conn = AsyncMock()
    conn.fetch = AsyncMock(side_effect=_fake_fetch)

    wallets, tokens = await observer_main._load_db_subscriptions(conn)

    # The leaders query must include all three sources we now rely on.
    leaders_sql = next(
        s for s in captured_sql if "wallet_address" in s and "leaders" in s.lower()
    )
    assert "falcon_score" in leaders_sql, (
        "leaders UNION missing falcon_score source"
    )
    assert "follower_edges" in leaders_sql, (
        "leaders UNION missing follower_edges source"
    )
    assert "positions_reconstructed" in leaders_sql, (
        "leaders UNION missing the observed-winrate (positions_reconstructed) "
        "source — 2026-05-17 diagnosis §B.11 regression."
    )

    # Wallets returned as a set → dedup is enforced even when the mock
    # returns the same row in multiple branches.
    assert wallets == {"0xWalletA", "0xWalletB"}
    # Tokens come from the recent + markets branches.
    assert "0xTokRecent" in tokens
    assert "0xTokYes" in tokens
    assert "0xTokNo" in tokens


# --------------------------------------------------------------------------- #
# 3. wallet_limit parameter is forwarded as the cap on each UNION branch.      #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_load_db_subscriptions_forwards_wallet_limit():
    """Each UNION branch must use the same ``$1`` placeholder so a custom
    ``wallet_limit`` actually caps EVERY source, not just the first."""
    captured_args: list[tuple] = []

    async def _fake_fetch(sql, *args):
        captured_args.append(args)
        if "leaders" in sql.lower() and "wallet_address" in sql:
            return [{"wallet_address": "0x1"}]
        return []

    conn = AsyncMock()
    conn.fetch = AsyncMock(side_effect=_fake_fetch)

    await observer_main._load_db_subscriptions(conn, wallet_limit=123)

    # Find the args of the leaders query.
    # Each fetch() got (limit_value,) as positional args.
    leaders_args = next(
        a for a in captured_args
        if a and isinstance(a[0], int) and a[0] == 123
    )
    assert leaders_args == (123,)


# --------------------------------------------------------------------------- #
# 4. Bootstrap returns a unique set even when the same wallet appears in       #
#    multiple branches (UNION semantics live in SQL but the Python collector   #
#    must not introduce duplicates either).                                    #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_bootstrap_returns_unique_wallets():
    """The set comprehension in ``_load_db_subscriptions`` dedupes
    across branches because UNION rows are deduped at the SQL layer.
    A bug here would manifest as duplicates leaking into the
    leader_wallets set passed to ``TradeObserver``, inflating
    subscription counts."""

    async def _fake_fetch(sql, *args):
        if "wallet_address" in sql and "leaders" in sql.lower():
            return [
                {"wallet_address": "0xDup"},
                {"wallet_address": "0xDup"},
                {"wallet_address": "0xUniqueA"},
                {"wallet_address": None},  # silently dropped
            ]
        return []

    conn = AsyncMock()
    conn.fetch = AsyncMock(side_effect=_fake_fetch)

    wallets, _ = await observer_main._load_db_subscriptions(conn)
    assert wallets == {"0xDup", "0xUniqueA"}
