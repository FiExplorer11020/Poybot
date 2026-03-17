import asyncio

from app.services.trade_executor import ExecutionRequest, TradeExecutor


def test_trade_executor_dry_run_mode() -> None:
    executor = TradeExecutor()
    out = asyncio.run(
        executor.execute(
            ExecutionRequest(
                market_id="m1",
                market_title="T",
                token_id="tok",
                side="BUY_YES",
                price=0.55,
                size=100,
                notional=55,
                risk_pct=0.5,
                expected_edge=0.02,
            )
        )
    )
    assert out["execution_mode"] == "dry_run"
    assert out["exchange_status"] == "SIMULATED"
