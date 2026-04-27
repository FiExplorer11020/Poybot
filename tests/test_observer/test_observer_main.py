from unittest.mock import AsyncMock

import pytest

from src.observer import main as observer_main


def test_extract_gamma_market_tokens_handles_json_and_lists():
    tokens = observer_main._extract_gamma_market_tokens(
        [
            {"clobTokenIds": '["tok-a", "tok-b"]'},
            {"clobTokenIds": ["tok-c", None]},
            {"clobTokenId": "tok-d"},
            {"clobTokenIds": "bad-json"},
        ]
    )

    assert tokens == {"tok-a", "tok-b", "tok-c", "tok-d"}


@pytest.mark.asyncio
async def test_load_db_subscriptions_returns_wallets_and_recent_tokens():
    conn = AsyncMock()
    conn.fetch = AsyncMock(
        side_effect=[
            [{"wallet_address": "0x1"}, {"wallet_address": "0x2"}],
            [{"token_id": "tok-trade"}],
            [{"token_yes": "tok-yes", "token_no": "tok-no"}],
        ]
    )

    wallets, tokens = await observer_main._load_db_subscriptions(conn)

    assert wallets == {"0x1", "0x2"}
    assert tokens == {"tok-trade", "tok-yes", "tok-no"}


def test_prioritize_subscription_tokens_caps_active_then_recent():
    tokens = observer_main._prioritize_subscription_tokens(
        active_tokens={"active-2", "active-1"},
        db_tokens={f"db-{i}" for i in range(10)},
        limit=5,
    )

    assert len(tokens) == 5
    assert {"active-1", "active-2"}.issubset(tokens)
