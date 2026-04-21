from datetime import datetime
from decimal import Decimal
from typing import Any

from src.economics.models import FeeSnapshot


def _decimal_or_none(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _extract_fee_rate(payload: dict[str, Any]) -> Decimal | None:
    fee_data = payload.get("fd") if isinstance(payload.get("fd"), dict) else {}
    raw_rate = (
        payload.get("feeRate")
        or payload.get("fee_rate")
        or payload.get("takerFeeRate")
        or fee_data.get("r")
    )
    rate = _decimal_or_none(raw_rate)
    if rate is None:
        return None

    exponent = _decimal_or_none(fee_data.get("e"))
    if exponent is not None and exponent >= 0 and rate > 1:
        rate = rate * (Decimal("10") ** -int(exponent))
    return rate


def fee_snapshot_from_clob_market_info(
    *,
    market_id: str,
    token_id: str,
    payload: dict[str, Any],
    captured_at: datetime,
    source: str = "clob_getClobMarketInfo",
) -> FeeSnapshot:
    """Build a canonical FeeSnapshot from CLOB market info.

    Polymarket fees are market-level and applied at match time. Enabled markets
    must expose fee params; disabled markets are represented explicitly with a
    zero fee rate instead of inventing a default.
    """
    fee_enabled = bool(payload.get("feesEnabled", payload.get("fees_enabled", False)))
    fee_rate = _extract_fee_rate(payload)

    if fee_enabled and fee_rate is None:
        raise ValueError("missing fee rate for fees-enabled market")
    if fee_rate is None:
        fee_rate = Decimal("0")
    if fee_rate < 0:
        raise ValueError("fee rate must be non-negative")

    return FeeSnapshot(
        market_id=market_id,
        token_id=token_id,
        fee_enabled=fee_enabled,
        fee_rate=fee_rate,
        maker_fee_rate=Decimal("0"),
        source=source,
        captured_at=captured_at,
        compatibility={
            "clob_v2": True,
            "fee_source_field": "fd.r" if isinstance(payload.get("fd"), dict) else "direct",
            "operator_set_at_match_time": True,
        },
    )
