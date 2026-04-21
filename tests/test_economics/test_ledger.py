from decimal import Decimal

from src.economics.ledger import PositionLedger


def test_fifo_partial_close_keeps_remaining_shares():
    ledger = PositionLedger()
    ledger.buy(
        market_id="m1",
        token_id="yes",
        price=Decimal("0.60"),
        size_shares=Decimal("1000"),
    )

    closed = ledger.sell(
        market_id="m1",
        token_id="yes",
        price=Decimal("0.70"),
        size_shares=Decimal("400"),
    )

    assert closed[0].gross_pnl_usdc == Decimal("40.000000")
    assert ledger.open_shares("m1", "yes") == Decimal("600")


def test_fifo_close_consumes_oldest_lot_first():
    ledger = PositionLedger()
    ledger.buy("m1", "yes", Decimal("0.40"), Decimal("100"))
    ledger.buy("m1", "yes", Decimal("0.60"), Decimal("100"))

    closed = ledger.sell("m1", "yes", Decimal("0.70"), Decimal("150"))

    assert closed[0].entry_price == Decimal("0.40")
    assert closed[0].size_shares == Decimal("100")
    assert closed[1].entry_price == Decimal("0.60")
    assert closed[1].size_shares == Decimal("50")
    assert ledger.open_shares("m1", "yes") == Decimal("50")


def test_resolution_closes_all_lots_at_resolution_price():
    ledger = PositionLedger()
    ledger.buy("m1", "yes", Decimal("0.30"), Decimal("100"))
    ledger.buy("m1", "yes", Decimal("0.50"), Decimal("50"))

    closed = ledger.resolve_market("m1", Decimal("1.00"))

    assert len(closed) == 2
    assert ledger.open_shares("m1", "yes") == Decimal("0")
