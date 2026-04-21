from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from src.backtest.models import BacktestBookSnapshot, BacktestCandle, BacktestMarket, BacktestTrade
from src.economics.fee_snapshots import fee_snapshot_from_clob_market_info


def _first(row: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return value
    return None


def _require(row: dict[str, Any], field_name: str, *keys: str) -> Any:
    value = _first(row, *keys)
    if value in (None, ""):
        raise ValueError(f"missing required {field_name}")
    return value


def _decimal(value: Any) -> Decimal:
    return Decimal(str(value))


def _timestamp(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    raw = str(value)
    if raw.isdigit():
        numeric = int(raw)
        if numeric > 10_000_000_000:
            numeric = numeric // 1000
        return datetime.fromtimestamp(numeric, tz=timezone.utc)
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


def normalize_trade_row(row: dict[str, Any]) -> BacktestTrade:
    market_id = str(_require(row, "trade field market_id", "condition_id", "market_id"))
    token_id = str(_require(row, "trade field token_id", "token_id", "asset_id"))
    event_ts = _timestamp(_require(row, "trade field timestamp", "timestamp", "time"))
    return BacktestTrade(
        leader_wallet=str(_require(row, "trade field leader_wallet", "proxy_wallet", "wallet")),
        market_id=market_id,
        token_id=token_id,
        side=str(_require(row, "trade field side", "side")).upper(),
        outcome=str(_first(row, "outcome", "outcome_name") or ""),
        price=_decimal(_require(row, "trade field price", "price")),
        size_shares=_decimal(_require(row, "trade field size", "size", "size_shares")),
        event_ts=event_ts,
        observed_ts=_timestamp(_first(row, "observed_ts", "observed_at") or event_ts),
        tx_hash=str(_require(row, "trade field tx_hash", "tx_hash", "transaction_hash")),
        source="falcon_556",
        raw=dict(row),
    )


def normalize_market_row(row: dict[str, Any]) -> BacktestMarket:
    market_id = str(_require(row, "market field market_id", "condition_id", "market_id"))
    yes_token, no_token = _extract_yes_no_tokens(row)
    captured_at = _timestamp(
        _first(row, "timestamp", "updated_at", "created_at") or datetime.now(tz=timezone.utc)
    )
    return BacktestMarket(
        market_id=market_id,
        question=str(_first(row, "question", "title", "slug") or market_id),
        category=str(_first(row, "category", "market_category") or "unknown"),
        yes_token_id=yes_token,
        no_token_id=no_token,
        volume_usdc=_decimal(_first(row, "volume_total", "volume", "volume_usdc") or "0"),
        fee_snapshot=fee_snapshot_from_clob_market_info(
            market_id=market_id,
            token_id=yes_token,
            payload=row,
            captured_at=captured_at,
            source="falcon_574",
        ),
    )


def _extract_yes_no_tokens(row: dict[str, Any]) -> tuple[str, str]:
    yes_direct = _first(row, "yes_token_id", "token_yes", "yes_token")
    no_direct = _first(row, "no_token_id", "token_no", "no_token")
    if yes_direct and no_direct:
        return str(yes_direct), str(no_direct)

    side_a = _first(row, "side_a_token_id", "token_a", "side_a_asset_id")
    side_b = _first(row, "side_b_token_id", "token_b", "side_b_asset_id")
    if side_a and side_b:
        return str(side_a), str(side_b)

    tokens = row.get("tokens") or row.get("outcomes") or []
    yes_token = None
    no_token = None
    if isinstance(tokens, list):
        for token in tokens:
            if not isinstance(token, dict):
                continue
            label = str(_first(token, "outcome", "name", "label") or "").lower()
            token_id = _first(token, "token_id", "asset_id", "id")
            if label == "yes":
                yes_token = token_id
            elif label == "no":
                no_token = token_id
    if not yes_token or not no_token:
        raise ValueError("missing required market field yes/no token ids")
    return str(yes_token), str(no_token)


def normalize_book_row(row: dict[str, Any]) -> BacktestBookSnapshot:
    return BacktestBookSnapshot(
        market_id=str(_require(row, "book field market_id", "condition_id", "market_id")),
        token_id=str(_require(row, "book field token_id", "token_id", "asset_id")),
        best_bid=_decimal(_require(row, "book field best_bid", "best_bid", "bid")),
        best_ask=_decimal(_require(row, "book field best_ask", "best_ask", "ask")),
        ts=_timestamp(_require(row, "book field timestamp", "timestamp", "time")),
        source="falcon_572",
    )


def normalize_candle_row(row: dict[str, Any]) -> BacktestCandle:
    start_ts = _timestamp(
        _require(
            row,
            "candle field start_time",
            "start_time",
            "start",
            "timestamp",
            "time",
            "candle_time",
        )
    )
    end_raw = _first(row, "end_time", "end", "close_time")
    end_ts = _timestamp(end_raw) if end_raw not in (None, "") else start_ts + timedelta(hours=1)
    return BacktestCandle(
        market_id=str(_require(row, "candle field market_id", "condition_id", "market_id")),
        token_id=str(_require(row, "candle field token_id", "token_id", "asset_id")),
        start_ts=start_ts,
        end_ts=end_ts,
        high=_decimal(_require(row, "candle field high", "high", "high_price")),
        low=_decimal(_require(row, "candle field low", "low", "low_price")),
        source="falcon_568",
    )
