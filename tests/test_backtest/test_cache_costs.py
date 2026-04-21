from datetime import datetime, timezone
from decimal import Decimal

from src.backtest.cache import BacktestCache
from src.backtest.costs.slippage import estimate_slippage_usdc
from src.backtest.costs.spread import CandleRange, estimate_spread_cost
from src.backtest.models import BacktestBookSnapshot


def test_cache_writes_parquet_and_deduplicates_records(tmp_path):
    cache = BacktestCache(tmp_path)
    records = [
        {"tx_hash": "0x1", "value": "first"},
        {"tx_hash": "0x1", "value": "duplicate"},
        {"tx_hash": "0x2", "value": "second"},
    ]

    path = cache.write_records("trades", "2026-04-20", records, dedupe_keys=("tx_hash",))
    loaded = cache.read_records("trades")

    assert path.name == "2026-04-20.parquet"
    assert path.exists()
    assert [row["tx_hash"] for row in loaded] == ["0x1", "0x2"]
    assert loaded[0]["value"] == "first"


def test_cache_ignores_missing_dedupe_keys_for_raw_provider_payloads(tmp_path):
    cache = BacktestCache(tmp_path)
    records = [
        {"transaction_hash": "0x1", "value": "first"},
        {"transaction_hash": "0x1", "value": "duplicate"},
    ]

    path = cache.write_records(
        "trades",
        "provider_payload",
        records,
        dedupe_keys=("tx_hash", "transaction_hash"),
    )
    loaded = cache.read_records("trades")

    assert path.exists()
    assert [row["transaction_hash"] for row in loaded] == ["0x1"]


def test_cache_manifest_tracks_done_shards(tmp_path):
    cache = BacktestCache(tmp_path)

    assert cache.is_done("falcon_556", "wallet=0x1/day=2026-04-20") is False
    cache.mark_done(
        "falcon_556",
        "wallet=0x1/day=2026-04-20",
        metadata={"rows": 12},
    )

    assert cache.is_done("falcon_556", "wallet=0x1/day=2026-04-20") is True
    manifest = cache.read_manifest("falcon_556")
    assert manifest["wallet=0x1/day=2026-04-20"]["rows"] == 12


def test_spread_cost_prefers_orderbook_then_candle_then_constant():
    now = datetime.now(tz=timezone.utc)
    book = BacktestBookSnapshot(
        market_id="m1",
        token_id="t1",
        best_bid=Decimal("0.49"),
        best_ask=Decimal("0.51"),
        ts=now,
        source="unit-book",
    )

    book_estimate = estimate_spread_cost(
        price=Decimal("0.50"),
        size_shares=Decimal("100"),
        book=book,
        category="crypto",
    )
    candle_estimate = estimate_spread_cost(
        price=Decimal("0.50"),
        size_shares=Decimal("100"),
        candle=CandleRange(high=Decimal("0.58"), low=Decimal("0.50")),
        category="crypto",
    )
    constant_estimate = estimate_spread_cost(
        price=Decimal("0.50"),
        size_shares=Decimal("100"),
        category="sports",
    )

    assert book_estimate.source == "orderbook"
    assert book_estimate.cost_usdc == Decimal("2.000000")
    assert candle_estimate.source == "candle"
    assert candle_estimate.spread_price == Decimal("0.024000")
    assert constant_estimate.source == "constant:sports"


def test_slippage_uses_square_root_impact_and_conservative_fallback():
    modeled = estimate_slippage_usdc(
        size_usdc=Decimal("100"),
        volume_24h_usdc=Decimal("10000"),
        volatility_24h=Decimal("0.20"),
        impact_k=Decimal("0.5"),
    )
    fallback = estimate_slippage_usdc(
        size_usdc=Decimal("100"),
        volume_24h_usdc=Decimal("0"),
        volatility_24h=Decimal("0.20"),
    )

    assert modeled.source == "sqrt_impact"
    assert modeled.cost_usdc == Decimal("1.000000")
    assert fallback.source == "constant"
    assert fallback.cost_usdc > Decimal("0")
