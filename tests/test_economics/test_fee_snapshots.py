from datetime import datetime, timezone
from decimal import Decimal

import pytest

from src.economics.fee_snapshots import fee_snapshot_from_clob_market_info


def test_fee_snapshot_parses_enabled_clob_fee_data():
    captured_at = datetime.now(tz=timezone.utc)

    snapshot = fee_snapshot_from_clob_market_info(
        market_id="m1",
        token_id="t1",
        payload={"feesEnabled": True, "fd": {"r": "4", "e": "2", "to": True}},
        captured_at=captured_at,
    )

    assert snapshot.market_id == "m1"
    assert snapshot.token_id == "t1"
    assert snapshot.fee_enabled is True
    assert snapshot.fee_rate == Decimal("0.04")
    assert snapshot.maker_fee_rate == Decimal("0")
    assert snapshot.source == "clob_getClobMarketInfo"


def test_fee_snapshot_uses_zero_rate_when_fees_disabled():
    snapshot = fee_snapshot_from_clob_market_info(
        market_id="m1",
        token_id="t1",
        payload={"feesEnabled": False},
        captured_at=datetime.now(tz=timezone.utc),
    )

    assert snapshot.fee_enabled is False
    assert snapshot.fee_rate == Decimal("0")


def test_fee_snapshot_rejects_enabled_market_without_fee_params():
    with pytest.raises(ValueError, match="missing fee rate"):
        fee_snapshot_from_clob_market_info(
            market_id="m1",
            token_id="t1",
            payload={"feesEnabled": True},
            captured_at=datetime.now(tz=timezone.utc),
        )
