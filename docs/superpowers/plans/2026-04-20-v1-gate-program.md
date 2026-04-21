# V1 Gate Program Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a paper-only, execution-ready V1 program that proves or refutes `leader_swing` and `micro_reactive` through separate economic gates.

**Architecture:** The plan starts with a canonical economic spine shared by backtest, paper trading and dry-run execution. `leader_swing` and `micro_reactive` then branch into separate proof tracks with separate data requirements, portfolios, risk gates and go/no-go criteria.

**Tech Stack:** Python, pytest, asyncpg, Redis, aiohttp, Polymarket/Falcon APIs, CLOB market WebSocket, Postgres migrations.

---

## Implementation Rules

- No real order submission in V1.
- Every PnL-bearing object must carry `economic_model_version`.
- Every decision/fill/report must carry `strategy_track`.
- Old raw events may be reused; old PnL, outcomes and learning labels may not.
- Backtest, paper and dry-run must use the same economics functions.
- A failing economics test blocks all strategy work.
- A failing data freshness gate blocks `micro_reactive`.

## File Structure

Create:
- `src/economics/__init__.py` - public exports for economics primitives.
- `src/economics/models.py` - canonical enums and dataclasses.
- `src/economics/fees.py` - official fee formulas and fee validation.
- `src/economics/pnl.py` - shares/notional conversions and net PnL calculations.
- `src/economics/ledger.py` - FIFO lot ledger, partial closes, merges and resolution.
- `src/execution/__init__.py` - execution venue public exports.
- `src/execution/venue.py` - `ExecutionVenue`, `PaperVenue`, `ClobVenueDryRun`.
- `scripts/invalidate_pre_v1_labels.py` - one-shot invalidation script for old labels.
- `docs/migrations/003_v1_economic_spine.sql` - schema changes for V1.
- `tests/test_economics/test_fees.py` - fee formula tests.
- `tests/test_economics/test_pnl.py` - PnL and unit conversion tests.
- `tests/test_economics/test_ledger.py` - FIFO, partial close, merge, resolution tests.
- `tests/test_execution/test_dry_run_venue.py` - safety tests for dry-run execution.
- `tests/test_safety/test_pre_v1_invalidation.py` - old-label invalidation tests.

Modify:
- `.env.example` - remove the exposed Falcon secret and leave an empty value.
- `scripts/run_all.py` - pass `RiskManager` into `PaperTrader`.
- `src/engine/paper_trader.py` - replace local PnL/fee math with economics module.
- `src/observer/position_tracker.py` - store and close positions by shares, not notional.
- `docs/PHASE_A_BACKTESTER_DESIGN.md` - mark old fee section superseded and point to canonical economics.
- `src/observer/websocket_client.py` - add V1 market event support for `micro_reactive` gate.
- `src/api/main.py` - expose real data-quality health, not static websocket status.

---

## Task 0: Baseline Verification And Dirty Reality Capture

**Files:**
- Read: repository root
- Output: terminal evidence only

- [ ] **Step 1: Capture current test baseline**

Run:

```bash
pytest -q
```

Expected: The suite may fail before V1 work. Record failing modules in the implementation notes and do not hide them behind V1 changes.

- [ ] **Step 2: Capture current lint baseline**

Run:

```bash
ruff check .
```

Expected: The repo may have pre-existing lint failures. Record them separately from new V1 failures.

- [ ] **Step 3: Check whether git is initialized**

Run:

```bash
git status --short
```

Expected in the current workspace: this may return `fatal: not a git repository`. If so, skip commit steps and mention this limitation in final status.

---

## Task 1: Phase 0 Schema For V1 Economic Reset

**Files:**
- Create: `docs/migrations/003_v1_economic_spine.sql`
- Test: `tests/test_safety/test_pre_v1_invalidation.py`

- [ ] **Step 1: Write migration content**

Create `docs/migrations/003_v1_economic_spine.sql` with:

```sql
BEGIN;

CREATE TABLE IF NOT EXISTS v1_label_invalidations (
    id BIGSERIAL PRIMARY KEY,
    target_table TEXT NOT NULL,
    target_id TEXT NOT NULL,
    invalidated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    reason TEXT NOT NULL,
    previous_economic_model_version TEXT,
    new_economic_model_version TEXT NOT NULL DEFAULT 'v1.0.0',
    raw_reference JSONB NOT NULL DEFAULT '{}'::jsonb
);

ALTER TABLE paper_trades
    ADD COLUMN IF NOT EXISTS strategy_track TEXT NOT NULL DEFAULT 'leader_swing',
    ADD COLUMN IF NOT EXISTS economic_model_version TEXT,
    ADD COLUMN IF NOT EXISTS invalidated_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS invalidated_reason TEXT,
    ADD COLUMN IF NOT EXISTS size_shares NUMERIC,
    ADD COLUMN IF NOT EXISTS entry_fee_usdc NUMERIC,
    ADD COLUMN IF NOT EXISTS exit_fee_usdc NUMERIC,
    ADD COLUMN IF NOT EXISTS spread_cost_usdc NUMERIC,
    ADD COLUMN IF NOT EXISTS slippage_usdc NUMERIC,
    ADD COLUMN IF NOT EXISTS gross_pnl_usdc NUMERIC,
    ADD COLUMN IF NOT EXISTS net_pnl_usdc NUMERIC,
    ADD COLUMN IF NOT EXISTS fill_audit JSONB NOT NULL DEFAULT '{}'::jsonb;

ALTER TABLE decision_log
    ADD COLUMN IF NOT EXISTS strategy_track TEXT,
    ADD COLUMN IF NOT EXISTS economic_model_version TEXT,
    ADD COLUMN IF NOT EXISTS invalidated_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS invalidated_reason TEXT,
    ADD COLUMN IF NOT EXISTS signal_audit JSONB NOT NULL DEFAULT '{}'::jsonb;

ALTER TABLE leader_profiles
    ADD COLUMN IF NOT EXISTS learning_invalidated_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS learning_invalidated_reason TEXT,
    ADD COLUMN IF NOT EXISTS economic_model_version TEXT;

ALTER TABLE positions_reconstructed
    ADD COLUMN IF NOT EXISTS size_shares NUMERIC,
    ADD COLUMN IF NOT EXISTS entry_fee_usdc NUMERIC,
    ADD COLUMN IF NOT EXISTS exit_fee_usdc NUMERIC,
    ADD COLUMN IF NOT EXISTS gross_pnl_usdc NUMERIC,
    ADD COLUMN IF NOT EXISTS net_pnl_usdc NUMERIC,
    ADD COLUMN IF NOT EXISTS economic_model_version TEXT,
    ADD COLUMN IF NOT EXISTS invalidated_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS invalidated_reason TEXT;

CREATE TABLE IF NOT EXISTS fee_snapshots (
    id BIGSERIAL PRIMARY KEY,
    market_id TEXT NOT NULL,
    token_id TEXT NOT NULL,
    fee_enabled BOOLEAN NOT NULL,
    fee_rate NUMERIC NOT NULL,
    maker_fee_rate NUMERIC NOT NULL DEFAULT 0,
    source TEXT NOT NULL,
    captured_at TIMESTAMPTZ NOT NULL,
    compatibility JSONB NOT NULL DEFAULT '{}'::jsonb,
    economic_model_version TEXT NOT NULL DEFAULT 'v1.0.0',
    UNIQUE (market_id, token_id, captured_at, source)
);

CREATE TABLE IF NOT EXISTS signal_audits (
    id BIGSERIAL PRIMARY KEY,
    decision_id BIGINT,
    strategy_track TEXT NOT NULL,
    market_id TEXT NOT NULL,
    token_id TEXT NOT NULL,
    accepted BOOLEAN NOT NULL,
    reject_reason TEXT,
    expected_edge_usdc NUMERIC,
    expected_net_edge_usdc NUMERIC,
    inputs JSONB NOT NULL DEFAULT '{}'::jsonb,
    cost_assumptions JSONB NOT NULL DEFAULT '{}'::jsonb,
    book_reference JSONB NOT NULL DEFAULT '{}'::jsonb,
    fee_snapshot_id BIGINT REFERENCES fee_snapshots(id),
    economic_model_version TEXT NOT NULL DEFAULT 'v1.0.0',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMIT;
```

- [ ] **Step 2: Add migration smoke test**

Create `tests/test_safety/test_pre_v1_invalidation.py` with a test that reads the SQL file and asserts it contains:

```python
from pathlib import Path


def test_v1_migration_contains_required_invalidation_surfaces():
    sql = Path("docs/migrations/003_v1_economic_spine.sql").read_text()

    required = [
        "v1_label_invalidations",
        "ALTER TABLE paper_trades",
        "ALTER TABLE decision_log",
        "ALTER TABLE leader_profiles",
        "ALTER TABLE positions_reconstructed",
        "fee_snapshots",
        "signal_audits",
        "economic_model_version",
        "strategy_track",
        "invalidated_at",
    ]

    for token in required:
        assert token in sql
```

- [ ] **Step 3: Run the migration smoke test**

Run:

```bash
pytest tests/test_safety/test_pre_v1_invalidation.py -q
```

Expected: pass.

---

## Task 2: Pre-V1 Label Invalidation Script

**Files:**
- Create: `scripts/invalidate_pre_v1_labels.py`
- Test: `tests/test_safety/test_pre_v1_invalidation.py`

- [ ] **Step 1: Add script behavior test**

Append to `tests/test_safety/test_pre_v1_invalidation.py`:

```python
def test_invalidation_script_targets_old_pnl_and_learning_labels():
    script = Path("scripts/invalidate_pre_v1_labels.py").read_text()

    required = [
        "paper_trades",
        "decision_log",
        "leader_profiles",
        "error_model_blob",
        "decision_learning",
        "v1_label_invalidations",
        "pre_v1_economic_reset",
    ]

    for token in required:
        assert token in script
```

- [ ] **Step 2: Run test and verify failure**

Run:

```bash
pytest tests/test_safety/test_pre_v1_invalidation.py::test_invalidation_script_targets_old_pnl_and_learning_labels -q
```

Expected: fail because the script does not exist yet.

- [ ] **Step 3: Implement script**

Create `scripts/invalidate_pre_v1_labels.py`:

```python
"""
Invalidate all pre-V1 economic labels while preserving raw events.

This script marks legacy PnL/outcome/learning data as unusable for V1 reports.
It does not delete raw trades or market data.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.database.connection import close_pool, get_db, initialize_pool
from src.config import settings

REASON = "pre_v1_economic_reset"
NEW_VERSION = "v1.0.0"


async def invalidate() -> None:
    await initialize_pool(
        dsn=settings.DATABASE_URL,
        min_size=settings.DB_POOL_MIN,
        max_size=settings.DB_POOL_MAX,
    )
    try:
        async with get_db() as conn:
            async with conn.transaction():
                await conn.execute(
                    """
                    INSERT INTO v1_label_invalidations
                        (target_table, target_id, reason, previous_economic_model_version, new_economic_model_version)
                    SELECT 'paper_trades', id::text, $1, economic_model_version, $2
                    FROM paper_trades
                    WHERE invalidated_at IS NULL
                      AND (economic_model_version IS NULL OR economic_model_version <> $2)
                    """,
                    REASON,
                    NEW_VERSION,
                )
                await conn.execute(
                    """
                    UPDATE paper_trades
                    SET invalidated_at = NOW(),
                        invalidated_reason = $1
                    WHERE invalidated_at IS NULL
                      AND (economic_model_version IS NULL OR economic_model_version <> $2)
                    """,
                    REASON,
                    NEW_VERSION,
                )
                await conn.execute(
                    """
                    INSERT INTO v1_label_invalidations
                        (target_table, target_id, reason, previous_economic_model_version, new_economic_model_version)
                    SELECT 'decision_log', id::text, $1, economic_model_version, $2
                    FROM decision_log
                    WHERE invalidated_at IS NULL
                      AND outcome IS NOT NULL
                    """,
                    REASON,
                    NEW_VERSION,
                )
                await conn.execute(
                    """
                    UPDATE decision_log
                    SET invalidated_at = NOW(),
                        invalidated_reason = $1,
                        outcome = NULL
                    WHERE invalidated_at IS NULL
                      AND outcome IS NOT NULL
                    """,
                    REASON,
                )
                await conn.execute(
                    """
                    UPDATE leader_profiles
                    SET learning_invalidated_at = NOW(),
                        learning_invalidated_reason = $1,
                        economic_model_version = $2,
                        profile_json = profile_json - 'decision_learning',
                        error_model_blob = NULL
                    WHERE learning_invalidated_at IS NULL
                       OR profile_json ? 'decision_learning'
                       OR error_model_blob IS NOT NULL
                    """,
                    REASON,
                    NEW_VERSION,
                )
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(invalidate())
```

- [ ] **Step 4: Run tests**

Run:

```bash
pytest tests/test_safety/test_pre_v1_invalidation.py -q
```

Expected: pass.

---

## Task 3: Secret Hygiene And Risk Wiring

**Files:**
- Modify: `.env.example`
- Modify: `scripts/run_all.py`
- Test: `tests/test_safety/test_pre_v1_invalidation.py`

- [ ] **Step 1: Add safety tests**

Append:

```python
def test_env_example_does_not_ship_falcon_secret():
    env_text = Path(".env.example").read_text()
    assert "FALCON_API_KEY=" in env_text
    assert "Bearer " not in env_text
    assert "sk-" not in env_text


def test_run_all_passes_risk_manager_to_paper_trader():
    source = Path("scripts/run_all.py").read_text()
    assert "risk_manager = RiskManager()" in source
    assert "risk_manager=risk_manager" in source
```

- [ ] **Step 2: Run tests and verify failure if currently unsafe**

Run:

```bash
pytest tests/test_safety/test_pre_v1_invalidation.py::test_env_example_does_not_ship_falcon_secret tests/test_safety/test_pre_v1_invalidation.py::test_run_all_passes_risk_manager_to_paper_trader -q
```

Expected: fail until `.env.example` and `scripts/run_all.py` are corrected.

- [ ] **Step 3: Fix `.env.example`**

Set:

```text
FALCON_API_KEY=
```

Do not include a real secret or bearer token.

- [ ] **Step 4: Wire risk manager**

Modify `scripts/run_all.py`:

```python
paper_trader = PaperTrader(
    redis_client=redis_client,
    confidence_engine=confidence,
    risk_manager=risk_manager,
)
```

- [ ] **Step 5: Run safety tests**

Run:

```bash
pytest tests/test_safety/test_pre_v1_invalidation.py -q
```

Expected: pass.

---

## Task 4: Canonical Economics Models

**Files:**
- Create: `src/economics/__init__.py`
- Create: `src/economics/models.py`
- Test: `tests/test_economics/test_pnl.py`

- [ ] **Step 1: Write failing model tests**

Create `tests/test_economics/test_pnl.py`:

```python
from decimal import Decimal

from src.economics.models import (
    ECONOMIC_MODEL_VERSION,
    LiquidityRole,
    OrderSide,
    StrategyTrack,
)


def test_required_enums_are_stable_strings():
    assert ECONOMIC_MODEL_VERSION == "v1.0.0"
    assert StrategyTrack.LEADER_SWING.value == "leader_swing"
    assert StrategyTrack.MICRO_REACTIVE.value == "micro_reactive"
    assert OrderSide.BUY.value == "BUY"
    assert OrderSide.SELL.value == "SELL"
    assert LiquidityRole.MAKER.value == "maker"
    assert LiquidityRole.TAKER.value == "taker"


def test_decimal_import_is_available_for_follow_up_tests():
    assert Decimal("1.00") == Decimal("1")
```

- [ ] **Step 2: Run test and verify failure**

Run:

```bash
pytest tests/test_economics/test_pnl.py::test_required_enums_are_stable_strings -q
```

Expected: fail because `src.economics.models` does not exist.

- [ ] **Step 3: Implement models**

Create `src/economics/models.py`:

```python
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any

ECONOMIC_MODEL_VERSION = "v1.0.0"


class StrategyTrack(str, Enum):
    LEADER_SWING = "leader_swing"
    MICRO_REACTIVE = "micro_reactive"


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class LiquidityRole(str, Enum):
    MAKER = "maker"
    TAKER = "taker"


@dataclass(frozen=True)
class FeeSnapshot:
    market_id: str
    token_id: str
    fee_enabled: bool
    fee_rate: Decimal
    source: str
    captured_at: datetime
    maker_fee_rate: Decimal = Decimal("0")
    compatibility: dict[str, Any] = field(default_factory=dict)
    economic_model_version: str = ECONOMIC_MODEL_VERSION


@dataclass(frozen=True)
class CanonicalTrade:
    market_id: str
    token_id: str
    side: OrderSide
    price: Decimal
    size_shares: Decimal
    notional_usdc: Decimal
    exchange_ts: datetime
    observed_ts: datetime
    source: str
    raw_ref: dict[str, Any] = field(default_factory=dict)
    economic_model_version: str = ECONOMIC_MODEL_VERSION


@dataclass(frozen=True)
class CanonicalFill:
    strategy_track: StrategyTrack
    market_id: str
    token_id: str
    side: OrderSide
    liquidity_role: LiquidityRole
    price: Decimal
    size_shares: Decimal
    notional_usdc: Decimal
    fee_usdc: Decimal
    spread_cost_usdc: Decimal = Decimal("0")
    slippage_usdc: Decimal = Decimal("0")
    audit: dict[str, Any] = field(default_factory=dict)
    economic_model_version: str = ECONOMIC_MODEL_VERSION
```

Create `src/economics/__init__.py`:

```python
from src.economics.models import (
    ECONOMIC_MODEL_VERSION,
    CanonicalFill,
    CanonicalTrade,
    FeeSnapshot,
    LiquidityRole,
    OrderSide,
    StrategyTrack,
)

__all__ = [
    "ECONOMIC_MODEL_VERSION",
    "CanonicalFill",
    "CanonicalTrade",
    "FeeSnapshot",
    "LiquidityRole",
    "OrderSide",
    "StrategyTrack",
]
```

- [ ] **Step 4: Run tests**

Run:

```bash
pytest tests/test_economics/test_pnl.py -q
```

Expected: pass.

---

## Task 5: Official Fee Model

**Files:**
- Create: `src/economics/fees.py`
- Test: `tests/test_economics/test_fees.py`

- [ ] **Step 1: Write failing fee tests**

Create `tests/test_economics/test_fees.py`:

```python
from decimal import Decimal

import pytest

from src.economics.fees import calculate_polymarket_fee
from src.economics.models import LiquidityRole


def test_taker_fee_uses_polymarket_binary_fee_formula():
    fee = calculate_polymarket_fee(
        shares=Decimal("1000"),
        price=Decimal("0.60"),
        fee_rate=Decimal("0.01"),
        liquidity_role=LiquidityRole.TAKER,
        fees_enabled=True,
    )
    assert fee == Decimal("2.400000")


def test_maker_fee_is_zero_by_default():
    fee = calculate_polymarket_fee(
        shares=Decimal("1000"),
        price=Decimal("0.60"),
        fee_rate=Decimal("0.01"),
        liquidity_role=LiquidityRole.MAKER,
        fees_enabled=True,
    )
    assert fee == Decimal("0.000000")


def test_fee_disabled_returns_zero():
    fee = calculate_polymarket_fee(
        shares=Decimal("1000"),
        price=Decimal("0.60"),
        fee_rate=Decimal("0.01"),
        liquidity_role=LiquidityRole.TAKER,
        fees_enabled=False,
    )
    assert fee == Decimal("0.000000")


@pytest.mark.parametrize("bad_price", [Decimal("-0.01"), Decimal("1.01")])
def test_fee_rejects_invalid_binary_prices(bad_price):
    with pytest.raises(ValueError):
        calculate_polymarket_fee(
            shares=Decimal("1000"),
            price=bad_price,
            fee_rate=Decimal("0.01"),
            liquidity_role=LiquidityRole.TAKER,
            fees_enabled=True,
        )
```

- [ ] **Step 2: Run test and verify failure**

Run:

```bash
pytest tests/test_economics/test_fees.py -q
```

Expected: fail because `src.economics.fees` does not exist.

- [ ] **Step 3: Implement fee model**

Create `src/economics/fees.py`:

```python
from decimal import Decimal, ROUND_HALF_UP

from src.economics.models import LiquidityRole

USD_QUANT = Decimal("0.000001")


def _to_decimal(value: Decimal | int | float | str) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def calculate_polymarket_fee(
    *,
    shares: Decimal | int | float | str,
    price: Decimal | int | float | str,
    fee_rate: Decimal | int | float | str,
    liquidity_role: LiquidityRole | str = LiquidityRole.TAKER,
    fees_enabled: bool = True,
) -> Decimal:
    shares_d = _to_decimal(shares)
    price_d = _to_decimal(price)
    fee_rate_d = _to_decimal(fee_rate)
    role = LiquidityRole(liquidity_role)

    if shares_d < 0:
        raise ValueError("shares must be non-negative")
    if price_d < 0 or price_d > 1:
        raise ValueError("binary price must be between 0 and 1")
    if fee_rate_d < 0:
        raise ValueError("fee_rate must be non-negative")
    if not fees_enabled or role == LiquidityRole.MAKER:
        return Decimal("0").quantize(USD_QUANT)

    fee = shares_d * fee_rate_d * price_d * (Decimal("1") - price_d)
    return fee.quantize(USD_QUANT, rounding=ROUND_HALF_UP)
```

- [ ] **Step 4: Run fee tests**

Run:

```bash
pytest tests/test_economics/test_fees.py -q
```

Expected: pass.

---

## Task 6: PnL And Shares/Notional Conversion

**Files:**
- Create: `src/economics/pnl.py`
- Test: `tests/test_economics/test_pnl.py`

- [ ] **Step 1: Add failing PnL tests**

Append:

```python
import pytest

from src.economics.pnl import calculate_long_pnl, shares_from_notional


def test_shares_from_notional_uses_entry_price():
    assert shares_from_notional(Decimal("200"), Decimal("0.50")) == Decimal("400.000000")


def test_long_yes_pnl_uses_shares_not_notional_as_multiplier():
    result = calculate_long_pnl(
        entry_price=Decimal("0.50"),
        exit_price=Decimal("0.60"),
        size_shares=Decimal("400"),
        entry_fee_usdc=Decimal("0"),
        exit_fee_usdc=Decimal("0"),
    )
    assert result.gross_pnl_usdc == Decimal("40.000000")
    assert result.net_pnl_usdc == Decimal("40.000000")


def test_long_pnl_subtracts_costs():
    result = calculate_long_pnl(
        entry_price=Decimal("0.50"),
        exit_price=Decimal("0.60"),
        size_shares=Decimal("400"),
        entry_fee_usdc=Decimal("1.00"),
        exit_fee_usdc=Decimal("1.20"),
        spread_cost_usdc=Decimal("0.50"),
        slippage_usdc=Decimal("0.30"),
    )
    assert result.gross_pnl_usdc == Decimal("40.000000")
    assert result.net_pnl_usdc == Decimal("37.000000")


def test_shares_from_notional_rejects_zero_price():
    with pytest.raises(ValueError):
        shares_from_notional(Decimal("200"), Decimal("0"))
```

- [ ] **Step 2: Run test and verify failure**

Run:

```bash
pytest tests/test_economics/test_pnl.py -q
```

Expected: fail because `src.economics.pnl` does not exist.

- [ ] **Step 3: Implement PnL functions**

Create `src/economics/pnl.py`:

```python
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP

USD_QUANT = Decimal("0.000001")


def _to_decimal(value: Decimal | int | float | str) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


@dataclass(frozen=True)
class PnLResult:
    gross_pnl_usdc: Decimal
    net_pnl_usdc: Decimal
    notional_usdc: Decimal
    pnl_pct: Decimal


def shares_from_notional(
    notional_usdc: Decimal | int | float | str,
    entry_price: Decimal | int | float | str,
) -> Decimal:
    notional = _to_decimal(notional_usdc)
    price = _to_decimal(entry_price)
    if price <= 0:
        raise ValueError("entry_price must be positive")
    if price > 1:
        raise ValueError("binary entry_price must be <= 1")
    if notional < 0:
        raise ValueError("notional_usdc must be non-negative")
    return (notional / price).quantize(USD_QUANT, rounding=ROUND_HALF_UP)


def calculate_long_pnl(
    *,
    entry_price: Decimal | int | float | str,
    exit_price: Decimal | int | float | str,
    size_shares: Decimal | int | float | str,
    entry_fee_usdc: Decimal | int | float | str = Decimal("0"),
    exit_fee_usdc: Decimal | int | float | str = Decimal("0"),
    spread_cost_usdc: Decimal | int | float | str = Decimal("0"),
    slippage_usdc: Decimal | int | float | str = Decimal("0"),
) -> PnLResult:
    entry = _to_decimal(entry_price)
    exit_ = _to_decimal(exit_price)
    shares = _to_decimal(size_shares)
    entry_fee = _to_decimal(entry_fee_usdc)
    exit_fee = _to_decimal(exit_fee_usdc)
    spread = _to_decimal(spread_cost_usdc)
    slippage = _to_decimal(slippage_usdc)

    if entry <= 0 or entry > 1:
        raise ValueError("entry_price must be in (0, 1]")
    if exit_ < 0 or exit_ > 1:
        raise ValueError("exit_price must be in [0, 1]")
    if shares < 0:
        raise ValueError("size_shares must be non-negative")

    notional = (entry * shares).quantize(USD_QUANT, rounding=ROUND_HALF_UP)
    gross = ((exit_ - entry) * shares).quantize(USD_QUANT, rounding=ROUND_HALF_UP)
    net = (gross - entry_fee - exit_fee - spread - slippage).quantize(
        USD_QUANT,
        rounding=ROUND_HALF_UP,
    )
    pnl_pct = Decimal("0")
    if notional > 0:
        pnl_pct = (net / notional).quantize(USD_QUANT, rounding=ROUND_HALF_UP)
    return PnLResult(
        gross_pnl_usdc=gross,
        net_pnl_usdc=net,
        notional_usdc=notional,
        pnl_pct=pnl_pct,
    )
```

- [ ] **Step 4: Export PnL functions**

Modify `src/economics/__init__.py` to export:

```python
from src.economics.pnl import PnLResult, calculate_long_pnl, shares_from_notional
```

and include the names in `__all__`.

- [ ] **Step 5: Run PnL tests**

Run:

```bash
pytest tests/test_economics/test_pnl.py -q
```

Expected: pass.

---

## Task 7: FIFO Ledger

**Files:**
- Create: `src/economics/ledger.py`
- Test: `tests/test_economics/test_ledger.py`

- [ ] **Step 1: Write failing ledger tests**

Create `tests/test_economics/test_ledger.py`:

```python
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
```

- [ ] **Step 2: Run test and verify failure**

Run:

```bash
pytest tests/test_economics/test_ledger.py -q
```

Expected: fail because `src.economics.ledger` does not exist.

- [ ] **Step 3: Implement ledger**

Create `src/economics/ledger.py` with:

```python
from dataclasses import dataclass
from decimal import Decimal

from src.economics.pnl import PnLResult, calculate_long_pnl


@dataclass
class OpenLot:
    market_id: str
    token_id: str
    entry_price: Decimal
    size_shares: Decimal


@dataclass(frozen=True)
class ClosedLot:
    market_id: str
    token_id: str
    entry_price: Decimal
    exit_price: Decimal
    size_shares: Decimal
    gross_pnl_usdc: Decimal
    net_pnl_usdc: Decimal


class PositionLedger:
    def __init__(self) -> None:
        self._lots: dict[tuple[str, str], list[OpenLot]] = {}

    def buy(
        self,
        market_id: str,
        token_id: str,
        price: Decimal,
        size_shares: Decimal,
    ) -> None:
        key = (market_id, token_id)
        self._lots.setdefault(key, []).append(
            OpenLot(
                market_id=market_id,
                token_id=token_id,
                entry_price=price,
                size_shares=size_shares,
            )
        )

    def sell(
        self,
        market_id: str,
        token_id: str,
        price: Decimal,
        size_shares: Decimal,
    ) -> list[ClosedLot]:
        key = (market_id, token_id)
        lots = self._lots.get(key, [])
        remaining = size_shares
        closed: list[ClosedLot] = []

        while remaining > 0 and lots:
            lot = lots[0]
            close_shares = min(remaining, lot.size_shares)
            pnl: PnLResult = calculate_long_pnl(
                entry_price=lot.entry_price,
                exit_price=price,
                size_shares=close_shares,
            )
            closed.append(
                ClosedLot(
                    market_id=market_id,
                    token_id=token_id,
                    entry_price=lot.entry_price,
                    exit_price=price,
                    size_shares=close_shares,
                    gross_pnl_usdc=pnl.gross_pnl_usdc,
                    net_pnl_usdc=pnl.net_pnl_usdc,
                )
            )
            lot.size_shares -= close_shares
            remaining -= close_shares
            if lot.size_shares == 0:
                lots.pop(0)

        if not lots and key in self._lots:
            del self._lots[key]
        return closed

    def resolve_market(self, market_id: str, resolution_price: Decimal) -> list[ClosedLot]:
        closed: list[ClosedLot] = []
        keys = [key for key in self._lots if key[0] == market_id]
        for _, token_id in keys:
            shares = self.open_shares(market_id, token_id)
            closed.extend(self.sell(market_id, token_id, resolution_price, shares))
        return closed

    def open_shares(self, market_id: str, token_id: str) -> Decimal:
        return sum(
            (lot.size_shares for lot in self._lots.get((market_id, token_id), [])),
            Decimal("0"),
        )
```

- [ ] **Step 4: Run ledger tests**

Run:

```bash
pytest tests/test_economics/test_ledger.py -q
```

Expected: pass.

---

## Task 8: PaperTrader Uses Canonical Economics

**Files:**
- Modify: `src/engine/paper_trader.py`
- Modify: `tests/test_engine/test_paper_trader.py`

- [ ] **Step 1: Fix test imports if needed**

Ensure `tests/test_engine/test_paper_trader.py` imports:

```python
from datetime import datetime, timezone
```

- [ ] **Step 2: Change PnL expectations**

Update the profitable close test:

```python
# size_usdc=200 at entry=0.50 means 400 shares.
# exit=0.60 means gross pnl=(0.60-0.50)*400=40.
assert trader.capital == pytest.approx(initial_capital + 40.0)
```

Update the stop-loss test:

```python
# size_usdc=200 at entry=0.50 means 400 shares.
# exit=0.46 means gross pnl=(0.46-0.50)*400=-16.
assert trader.capital == pytest.approx(settings.PAPER_CAPITAL_USDC - 16.0)
```

- [ ] **Step 3: Run paper tests and verify failure**

Run:

```bash
pytest tests/test_engine/test_paper_trader.py::TestCloseTrade -q
```

Expected: fail while `paper_trader.py` still multiplies price delta by notional.

- [ ] **Step 4: Implement canonical shares in open/close**

Modify `src/engine/paper_trader.py`:

```python
from src.economics.fees import calculate_polymarket_fee
from src.economics.models import ECONOMIC_MODEL_VERSION, LiquidityRole, StrategyTrack
from src.economics.pnl import calculate_long_pnl, shares_from_notional
```

Extend `OpenPaperTrade`:

```python
size_shares: float = 0.0
entry_fee_usdc: float = 0.0
economic_model_version: str = ECONOMIC_MODEL_VERSION
strategy_track: str = StrategyTrack.LEADER_SWING.value
```

In `open_trade`, after entry price:

```python
strategy_track = trade_context.get("strategy_track") or decision.get("strategy_track") or StrategyTrack.LEADER_SWING.value
size_shares = float(shares_from_notional(size_usdc, entry_price))
fee_rate = await self._get_fee_rate(market_id)
entry_fee = float(
    calculate_polymarket_fee(
        shares=size_shares,
        price=entry_price,
        fee_rate=fee_rate,
        liquidity_role=LiquidityRole.TAKER,
        fees_enabled=True,
    )
)
```

In `close_trade`, replace PnL math with:

```python
exit_fee = float(
    calculate_polymarket_fee(
        shares=trade.size_shares,
        price=exit_price,
        fee_rate=trade.fee_rate_pct,
        liquidity_role=LiquidityRole.TAKER,
        fees_enabled=True,
    )
)
pnl = calculate_long_pnl(
    entry_price=trade.entry_price,
    exit_price=exit_price,
    size_shares=trade.size_shares,
    entry_fee_usdc=trade.entry_fee_usdc,
    exit_fee_usdc=exit_fee,
)
pnl_usdc = float(pnl.net_pnl_usdc)
gross_pnl_usdc = float(pnl.gross_pnl_usdc)
```

For `direction == "no"`, use the same long-token math because FADE buys the opposite NO token. Do not model short exposure by subtracting price movement on the YES token.

- [ ] **Step 5: Persist new columns**

Include in insert/update SQL:

```sql
strategy_track, economic_model_version, size_shares, entry_fee_usdc
```

and on close:

```sql
exit_fee_usdc=$6, gross_pnl_usdc=$7, net_pnl_usdc=$8
```

- [ ] **Step 6: Run paper tests**

Run:

```bash
pytest tests/test_engine/test_paper_trader.py::TestCloseTrade -q
```

Expected: pass.

---

## Task 9: PositionTracker Uses Shares, Not Notional, For Closes

**Files:**
- Modify: `src/observer/position_tracker.py`
- Modify: `tests/test_observer/test_position_tracker.py`

- [ ] **Step 1: Update trade fixtures to include shares**

Modify `_buy_trade` and `_sell_trade` to accept `size_shares`, defaulting to:

```python
size_shares="1000"
```

Return payloads with both:

```python
"size_usdc": size_usdc,
"size_shares": size_shares,
```

- [ ] **Step 2: Update expected PnL**

For BUY 1000 shares at 0.60 and SELL 1000 shares at 0.70:

```python
assert abs(float(pnl_usdc) - 100.0) < 0.02
```

With fee rate `0.01`, official fee formula:

```python
# entry_fee = 1000 * 0.01 * 0.60 * 0.40 = 2.40
# exit_fee = 1000 * 0.01 * 0.70 * 0.30 = 2.10
# net = 100 - 2.40 - 2.10 = 95.50
assert abs(float(pnl_usdc) - 95.50) < 0.02
```

- [ ] **Step 3: Run tests and verify failure**

Run:

```bash
pytest tests/test_observer/test_position_tracker.py::test_pnl_calculation_profit tests/test_observer/test_position_tracker.py::test_fee_deduction_in_pnl -q
```

Expected: fail while tracker closes by notional.

- [ ] **Step 4: Implement share-aware tracker**

Modify `OpenPosition`:

```python
size_shares: Decimal
shares_remaining: Decimal
```

In `on_trade`:

```python
size_shares_raw = trade.get("size_shares")
if size_shares_raw is None:
    size_shares = size_usdc / price if price > 0 else Decimal("0")
else:
    size_shares = Decimal(str(size_shares_raw))
```

Use `shares_remaining` for FIFO close decisions. Use `calculate_long_pnl` and `calculate_polymarket_fee` for PnL.

- [ ] **Step 5: Persist share and fee columns**

Insert into `positions_reconstructed`:

```sql
size_usdc, size_shares, entry_fee_usdc, exit_fee_usdc, gross_pnl_usdc, net_pnl_usdc,
economic_model_version
```

Keep `pnl_usdc` populated with `net_pnl_usdc` for backward compatibility.

- [ ] **Step 6: Run tracker tests**

Run:

```bash
pytest tests/test_observer/test_position_tracker.py -q
```

Expected: pass or expose unrelated pre-existing observer test failures separately.

---

## Task 10: Dry-Run Execution Venue

**Files:**
- Create: `src/execution/__init__.py`
- Create: `src/execution/venue.py`
- Test: `tests/test_execution/test_dry_run_venue.py`

- [ ] **Step 1: Write failing dry-run tests**

Create `tests/test_execution/test_dry_run_venue.py`:

```python
from decimal import Decimal

import pytest

from src.economics.models import OrderSide, StrategyTrack
from src.execution.venue import ClobVenueDryRun, DryRunOrder


@pytest.mark.asyncio
async def test_clob_dry_run_never_submits_real_order():
    venue = ClobVenueDryRun()
    order = DryRunOrder(
        strategy_track=StrategyTrack.LEADER_SWING,
        market_id="m1",
        token_id="t1",
        side=OrderSide.BUY,
        size_shares=Decimal("100"),
        limit_price=Decimal("0.55"),
        client_order_id="test-1",
        metadata={"reason": "unit-test"},
    )

    receipt = await venue.submit_order(order)

    assert receipt.dry_run is True
    assert receipt.would_submit is False
    assert receipt.accepted is True
    assert receipt.order == order
    assert receipt.reason == "dry_run_only"
```

- [ ] **Step 2: Run test and verify failure**

Run:

```bash
pytest tests/test_execution/test_dry_run_venue.py -q
```

Expected: fail because execution module does not exist.

- [ ] **Step 3: Implement venue**

Create `src/execution/venue.py`:

```python
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol

from src.economics.models import OrderSide, StrategyTrack


@dataclass(frozen=True)
class DryRunOrder:
    strategy_track: StrategyTrack
    market_id: str
    token_id: str
    side: OrderSide
    size_shares: Decimal
    limit_price: Decimal
    client_order_id: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DryRunReceipt:
    accepted: bool
    venue: str
    dry_run: bool
    would_submit: bool
    order: DryRunOrder
    reason: str


class ExecutionVenue(Protocol):
    async def submit_order(self, order: DryRunOrder) -> DryRunReceipt:
        ...


class ClobVenueDryRun:
    venue_name = "clob_dry_run"

    async def submit_order(self, order: DryRunOrder) -> DryRunReceipt:
        return DryRunReceipt(
            accepted=True,
            venue=self.venue_name,
            dry_run=True,
            would_submit=False,
            order=order,
            reason="dry_run_only",
        )


class PaperVenue(ClobVenueDryRun):
    venue_name = "paper"
```

Create `src/execution/__init__.py`:

```python
from src.execution.venue import (
    ClobVenueDryRun,
    DryRunOrder,
    DryRunReceipt,
    ExecutionVenue,
    PaperVenue,
)

__all__ = [
    "ClobVenueDryRun",
    "DryRunOrder",
    "DryRunReceipt",
    "ExecutionVenue",
    "PaperVenue",
]
```

- [ ] **Step 4: Run dry-run tests**

Run:

```bash
pytest tests/test_execution/test_dry_run_venue.py -q
```

Expected: pass.

---

## Task 11: Phase A Correction For `leader_swing`

**Files:**
- Modify: `docs/PHASE_A_BACKTESTER_DESIGN.md`
- Later Create: `src/backtest/data_loader.py`
- Later Create: `src/backtest/engine.py`
- Later Create: `src/backtest/report.py`

- [ ] **Step 1: Mark obsolete fee model**

Replace the old section that uses:

```text
fees_usdc = size_usdc x fee_rate_pct x 2
```

with:

```text
The old notional-percentage fee model is superseded by the canonical V1 economics module.
Every backtest fill must calculate taker fees as:

fee_usdc = shares * fee_rate * price * (1 - price)

Backtests may not compute PnL directly. They must call src.economics.pnl and src.economics.fees.
```

- [ ] **Step 2: Add Phase A boundary**

Add:

```text
Phase A only gates leader_swing. It must not be used to validate micro_reactive.
micro_reactive requires the separate live orderbook capture gate before any serious backtest claim.
```

- [ ] **Step 3: Add report requirements**

Ensure Phase A report requires:

```text
strategy_track=leader_swing
economic_model_version
baseline comparison
cost sensitivity
trade concentration
old labels excluded
```

- [ ] **Step 4: Add doc smoke test**

Add to `tests/test_safety/test_pre_v1_invalidation.py`:

```python
def test_phase_a_doc_points_to_canonical_economics():
    doc = Path("docs/PHASE_A_BACKTESTER_DESIGN.md").read_text()
    assert "canonical V1 economics" in doc
    assert "leader_swing" in doc
    assert "micro_reactive" in doc
    assert "shares * fee_rate * price * (1 - price)" in doc
```

- [ ] **Step 5: Run doc test**

Run:

```bash
pytest tests/test_safety/test_pre_v1_invalidation.py::test_phase_a_doc_points_to_canonical_economics -q
```

Expected: pass.

---

## Task 12: Micro Reactive Data Gate

**Files:**
- Modify: `src/observer/websocket_client.py`
- Create: `tests/test_observer/test_market_ws_parser_v1.py`
- Later Create: `src/observer/book_state.py`

- [ ] **Step 1: Write parser tests for V1 market events**

Create `tests/test_observer/test_market_ws_parser_v1.py` with fixtures for:

```python
EVENT_TYPES = [
    "book",
    "price_change",
    "last_trade_price",
    "best_bid_ask",
    "tick_size_change",
    "market_resolved",
]
```

Each fixture must assert:
- event type is recognized ;
- token id is extracted ;
- timestamp is preserved ;
- raw payload is retained ;
- unknown event types are rejected with a reason, not silently treated as trades.

- [ ] **Step 2: Run parser tests and verify failure**

Run:

```bash
pytest tests/test_observer/test_market_ws_parser_v1.py -q
```

Expected: fail until parser support exists.

- [ ] **Step 3: Add market event parser**

Implement a focused parser function in `src/observer/websocket_client.py` or a new `src/observer/market_events.py`:

```python
def parse_market_event(payload: dict) -> ParsedMarketEvent:
    ...
```

The returned object must include:
- `event_type` ;
- `token_id` ;
- `market_id` if present ;
- `exchange_ts` ;
- `observed_ts` ;
- `raw_payload` ;
- `reject_reason`.

- [ ] **Step 4: Add subscription compatibility**

Ensure market websocket subscriptions support CLOB V2 event coverage for:

```text
book
price_change
last_trade_price
best_bid_ask
tick_size_change
market_resolved
```

- [ ] **Step 5: Run parser tests**

Run:

```bash
pytest tests/test_observer/test_market_ws_parser_v1.py -q
```

Expected: pass.

---

## Task 13: Data Quality Health

**Files:**
- Modify: `src/api/main.py`
- Later Create: `src/monitoring/data_quality.py`
- Test: create or extend API tests if present

- [ ] **Step 1: Define data quality fields**

Health response must expose:

```json
{
  "websocket_connected": true,
  "last_message_age_s": 1.2,
  "book_age_p95_s": 2.4,
  "fee_snapshot_coverage_pct": 98.0,
  "token_map_coverage_pct": 100.0,
  "rejected_signals_1h": {
    "stale_book": 3,
    "missing_fee": 1,
    "missing_token_map": 0
  }
}
```

- [ ] **Step 2: Replace static websocket health**

Remove static values like:

```python
"websocket": True
```

and source health from Redis/database state.

- [ ] **Step 3: Add tests**

Test that health output includes all V1 fields and does not claim websocket healthy when no heartbeat exists.

- [ ] **Step 4: Run API health tests**

Run:

```bash
pytest tests -q -k "health or data_quality"
```

Expected: V1 health tests pass. Pre-existing unrelated failures must be listed separately.

---

## Task 14: Strategy Track Isolation

**Files:**
- Modify: `src/engine/paper_trader.py`
- Modify: `src/engine/risk_manager.py`
- Modify: report/dashboard modules discovered by `rg "paper_trades|decision_log|pnl_usdc"`
- Test: add focused tests under `tests/test_engine`

- [ ] **Step 1: Search all PnL aggregations**

Run:

```bash
rg "pnl_usdc|paper_trades|decision_log|outcome|leader_profiles" src tests scripts
```

Expected: list every aggregation that could mix tracks or old labels.

- [ ] **Step 2: Add tests for separate portfolios**

Add tests proving:
- `leader_swing` and `micro_reactive` have separate budget accounting ;
- combined PnL is not used as a go/no-go metric ;
- missing `strategy_track` defaults only for legacy compatibility and is marked as legacy.

- [ ] **Step 3: Implement strategy track propagation**

Ensure `strategy_track` is present in:
- open trade ;
- close event ;
- Redis event ;
- decision log update ;
- signal audit ;
- paper report.

- [ ] **Step 4: Run engine tests**

Run:

```bash
pytest tests/test_engine -q
```

Expected: V1 strategy track tests pass.

---

## Task 15: Paper V1 Fill Audit

**Files:**
- Modify: `src/engine/paper_trader.py`
- Create: `src/engine/signal_audit.py`
- Test: `tests/test_engine/test_paper_trader.py`

- [ ] **Step 1: Add reject tests**

Add tests proving `PaperTrader.open_trade` rejects when:
- missing fee snapshot ;
- missing token map ;
- stale book ;
- unresolved price source ;
- size unit ambiguity.

- [ ] **Step 2: Add fill audit tests**

Add tests proving accepted paper fills store:
- strategy track ;
- economic model version ;
- book reference ;
- fee snapshot reference ;
- expected edge ;
- expected net edge ;
- simulated fill price ;
- reject reason is null.

- [ ] **Step 3: Implement `SignalAudit` helper**

Create `src/engine/signal_audit.py` with a dataclass or builder that returns JSON-serializable audit payloads.

- [ ] **Step 4: Wire into PaperTrader**

Every accepted or rejected signal must produce a signal audit record or audit payload.

- [ ] **Step 5: Run paper tests**

Run:

```bash
pytest tests/test_engine/test_paper_trader.py -q
```

Expected: pass for V1 paper behavior.

---

## Task 16: Backtest Gate A `leader_swing`

**Files:**
- Create: `src/backtest/__init__.py`
- Create: `src/backtest/data_loader.py`
- Create: `src/backtest/engine.py`
- Create: `src/backtest/report.py`
- Create: `scripts/backtest_leader_swing.py`
- Test: `tests/test_backtest/test_leader_swing_gate.py`

- [ ] **Step 1: Implement minimum data contract tests**

Tests must assert:
- no lookahead: features at `t` cannot use events after `t - observation_lag`;
- old invalidated labels are excluded ;
- every simulated fill calls canonical economics ;
- every result has `strategy_track="leader_swing"`.

- [ ] **Step 2: Implement data loader**

Loader must fetch or read cached:
- leader trades ;
- market metadata ;
- fee snapshots ;
- optional book snapshots ;
- fallback candles.

Every fallback must be tagged in output.

- [ ] **Step 3: Implement event replay**

Replay must support:
- observation lag ;
- leader entry ;
- leader exit ;
- timeout ;
- resolution ;
- stop/take rules ;
- costs ;
- baselines.

- [ ] **Step 4: Implement report**

Report must include:
- net PnL ;
- gross PnL ;
- cost bridge ;
- Sharpe net ;
- max drawdown ;
- trade concentration ;
- baseline comparison ;
- cost sensitivity ;
- OOS split dates.

- [ ] **Step 5: Run backtest tests**

Run:

```bash
pytest tests/test_backtest/test_leader_swing_gate.py -q
```

Expected: pass.

---

## Task 17: Backtest/Pilot Gate B `micro_reactive`

**Files:**
- Create: `src/observer/book_state.py`
- Create: `src/backtest/micro_replay.py`
- Create: `scripts/capture_micro_reactive_pilot.py`
- Test: `tests/test_backtest/test_micro_reactive_gate.py`

- [ ] **Step 1: Implement book state tests**

Tests must assert:
- book updates are ordered by timestamp ;
- duplicates are ignored ;
- stale book is rejected ;
- best bid/ask is coherent ;
- spread/depth are computed.

- [ ] **Step 2: Implement `BookState`**

`BookState` must expose:
- `best_bid` ;
- `best_ask` ;
- `mid` ;
- `spread` ;
- `depth_at_bps` ;
- `last_update_age_s` ;
- `is_stale(max_age_s)`.

- [ ] **Step 3: Implement pilot capture script**

The pilot script must persist:
- raw market events ;
- normalized book snapshots ;
- latency measurements ;
- reject reasons ;
- uptime windows.

- [ ] **Step 4: Implement micro replay**

Replay must use persisted book states only. It must not execute against midpoint or last price alone.

- [ ] **Step 5: Run micro gate tests**

Run:

```bash
pytest tests/test_backtest/test_micro_reactive_gate.py -q
```

Expected: pass.

---

## Task 18: Final V1 Go/No-Go Report

**Files:**
- Create: `scripts/v1_go_no_go_report.py`
- Create: `docs/reports/` on first generated report
- Test: `tests/test_reporting/test_v1_go_no_go.py`

- [ ] **Step 1: Define report tests**

Tests must assert report includes:
- `leader_swing` gate status ;
- `micro_reactive` gate status ;
- old-label exclusion statement ;
- economic model version ;
- cost sensitivity ;
- data quality ;
- paper period ;
- explicit disabled tracks.

- [ ] **Step 2: Implement report script**

Script must read backtest, replay and paper outputs and write:
- JSON machine-readable report ;
- Markdown human-readable report.

- [ ] **Step 3: Add hard go/no-go logic**

Rules:
- if no track passes, status is `NO_GO`;
- if one track passes, status is `GO_FOR_TRACK_ONLY`;
- if a track lacks data quality proof, it is `DISABLED_DATA_INSUFFICIENT`;
- combined PnL cannot override failed track gates.

- [ ] **Step 4: Run report tests**

Run:

```bash
pytest tests/test_reporting/test_v1_go_no_go.py -q
```

Expected: pass.

---

## Task 19: Full Verification

**Files:**
- All V1 files

- [ ] **Step 1: Run economics suite**

Run:

```bash
pytest tests/test_economics -q
```

Expected: pass.

- [ ] **Step 2: Run execution safety suite**

Run:

```bash
pytest tests/test_execution tests/test_safety -q
```

Expected: pass.

- [ ] **Step 3: Run engine and observer suites**

Run:

```bash
pytest tests/test_engine tests/test_observer -q
```

Expected: V1 tests pass. Any unrelated pre-existing failures must be listed with file names.

- [ ] **Step 4: Run full pytest**

Run:

```bash
pytest -q
```

Expected: pass before declaring V1 implementation complete. If pre-existing failures remain, do not claim full completion.

- [ ] **Step 5: Run lint**

Run:

```bash
ruff check .
```

Expected: pass before declaring branch complete. If existing lint debt remains, separate it from V1 regressions.

---

## Execution Order Summary

1. Baseline reality capture.
2. Migration and invalidation.
3. Secret hygiene and risk wiring.
4. Economics models.
5. Fee model.
6. PnL model.
7. Ledger.
8. PaperTrader economics.
9. PositionTracker shares.
10. Dry-run venue.
11. Phase A doc correction.
12. Micro event parser.
13. Data quality health.
14. Strategy track isolation.
15. Paper V1 fill audit.
16. `leader_swing` gate.
17. `micro_reactive` gate.
18. V1 go/no-go report.
19. Full verification.

## Stop Conditions

Stop and reassess if:
- canonical economics tests fail ;
- official fees cannot be sourced or snapshotted ;
- token mapping is ambiguous ;
- `micro_reactive` book freshness fails the pilot gate ;
- `leader_swing` only works before costs ;
- paper PnL diverges materially from backtest/replay ;
- dry-run execution has any path that can submit real orders.
