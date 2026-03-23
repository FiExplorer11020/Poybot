import pytest

from app.services.spread_arb_scanner import (
    MarketPair,
    SpreadArbScanner,
    TopOfBookData,
    scan_universe,
)


def test_scan_detects_spread_arb_opportunity() -> None:
    scanner = SpreadArbScanner()

    opportunity = scanner.scan(
        yes_book=TopOfBookData(market_id="m1", ask=0.42, liquidity=500),
        no_book=TopOfBookData(market_id="m1", ask=0.55, liquidity=350),
    )

    assert opportunity is not None
    assert opportunity.market_id == "m1"
    assert opportunity.combined_cost == pytest.approx(0.97)
    assert opportunity.gross_profit == pytest.approx(0.03)
    assert opportunity.net_profit == pytest.approx(0.028448)
    assert opportunity.net_profit_pct == pytest.approx(2.932784, rel=1e-6)
    assert opportunity.max_size_usdc == pytest.approx(350)


def test_scan_rejects_when_fees_eat_the_edge() -> None:
    scanner = SpreadArbScanner()

    opportunity = scanner.scan(
        yes_book=TopOfBookData(market_id="m2", ask=0.497, liquidity=200),
        no_book=TopOfBookData(market_id="m2", ask=0.5, liquidity=250),
    )

    assert opportunity is None


def test_scan_rejects_when_combined_cost_exceeds_one() -> None:
    scanner = SpreadArbScanner()

    opportunity = scanner.scan(
        yes_book=TopOfBookData(market_id="m3", ask=0.56, liquidity=100),
        no_book=TopOfBookData(market_id="m3", ask=0.45, liquidity=100),
    )

    assert opportunity is None


def test_scan_universe_returns_sorted_opportunities() -> None:
    universe = [
        MarketPair(
            market_id="best",
            yes_book=TopOfBookData(market_id="best", ask=0.40, liquidity=150),
            no_book=TopOfBookData(market_id="best", ask=0.55, liquidity=120),
        ),
        MarketPair(
            market_id="good",
            yes_book=TopOfBookData(market_id="good", ask=0.42, liquidity=150),
            no_book=TopOfBookData(market_id="good", ask=0.55, liquidity=120),
        ),
        MarketPair(
            market_id="reject",
            yes_book=TopOfBookData(market_id="reject", ask=0.55, liquidity=120),
            no_book=TopOfBookData(market_id="reject", ask=0.46, liquidity=120),
        ),
    ]

    opportunities = scan_universe(universe)

    assert [opportunity.market_id for opportunity in opportunities] == ["best", "good"]
    assert opportunities[0].net_profit_pct > opportunities[1].net_profit_pct
