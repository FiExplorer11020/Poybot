from types import SimpleNamespace

import pytest

from app.services.adaptive_strategy import PortfolioState, RiskConfig
from app.services.risk_guard import APIFailureHaltException, DrawdownHaltException, RiskGuard


class DummyClob:
    def __init__(self, response: dict | None = None, error: Exception | None = None) -> None:
        self.response = response or {"canceled": []}
        self.error = error
        self.calls = 0

    async def cancel_all_orders(self, headers=None, endpoint="/cancel-all") -> dict:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.response


class DummyExecutor:
    def __init__(self, clob: DummyClob | None = None) -> None:
        self.clob = clob or DummyClob()
        self.settings = SimpleNamespace(
            polymarket_api_key=None,
            polymarket_api_secret=None,
            polymarket_api_passphrase=None,
        )


@pytest.mark.anyio
async def test_check_drawdown_allows_loss_below_threshold() -> None:
    guard = RiskGuard(RiskConfig(max_drawdown_stop_pct=0.10), DummyExecutor())

    await guard.check_drawdown(
        PortfolioState(
            equity=22_750.0,
            capital_in_trade=0.0,
            total_pnl=-2_250.0,
        )
    )


@pytest.mark.anyio
async def test_check_drawdown_halts_above_threshold() -> None:
    guard = RiskGuard(RiskConfig(max_drawdown_stop_pct=0.10), DummyExecutor())

    with pytest.raises(DrawdownHaltException) as exc_info:
        await guard.check_drawdown(
            PortfolioState(
                equity=22_250.0,
                capital_in_trade=0.0,
                total_pnl=-2_750.0,
            )
        )

    assert exc_info.value.current_drawdown_pct == pytest.approx(0.11)
    assert exc_info.value.threshold_pct == pytest.approx(0.10)


@pytest.mark.anyio
async def test_check_api_health_halts_after_three_failures() -> None:
    guard = RiskGuard(RiskConfig(), DummyExecutor())

    guard.record_api_failure(RuntimeError("first"))
    await guard.check_api_health()
    guard.record_api_failure(RuntimeError("second"))
    await guard.check_api_health()
    guard.record_api_failure(RuntimeError("third"))

    with pytest.raises(APIFailureHaltException) as exc_info:
        await guard.check_api_health()

    assert exc_info.value.consecutive_failures == 3

    guard.record_api_success()
    await guard.check_api_health()


@pytest.mark.anyio
async def test_cancel_all_open_orders_never_raises() -> None:
    guard = RiskGuard(
        RiskConfig(),
        DummyExecutor(clob=DummyClob(error=RuntimeError("cancel failed"))),
    )

    result = await guard.cancel_all_open_orders()

    assert result["cancelled"] == 0
    assert result["errors"] == ["cancel failed"]
