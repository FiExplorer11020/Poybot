import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from src.backtest.engine import _compute_metrics
from src.backtest.models import BacktestFill, BacktestRun

MIN_REAL_COST_SOURCE_PCT = Decimal("0.70")
MIN_BOOK_SOURCE_PCT = Decimal("0.40")
MAX_CONSTANT_FALLBACK_PCT = Decimal("0.30")


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    return value


def build_gate_report(
    primary: BacktestRun,
    baselines: dict[str, BacktestRun],
    *,
    min_sharpe: Decimal = Decimal("0.5"),
) -> dict[str, Any]:
    data_quality = _data_quality(primary.fills)
    eligible_fills = _eligible_gate_fills(primary.fills)
    score_metrics = _compute_metrics(eligible_fills)
    baseline_comparison = {
        name: {
            "net_pnl_usdc": run.metrics["net_pnl_usdc"],
            "sharpe_net": run.metrics["sharpe_net"],
            "total_trades": run.metrics["total_trades"],
        }
        for name, run in baselines.items()
    }
    fade_pnl = baselines.get("fade_all", primary).metrics["net_pnl_usdc"]
    data_sufficient = _data_sufficient(data_quality)
    economics_passed = (
        score_metrics["net_pnl_usdc"] > Decimal("0")
        and score_metrics["sharpe_net"] > min_sharpe
        and score_metrics["net_pnl_usdc"] > fade_pnl
        and score_metrics["total_trades"] > 0
    )
    gate_status = (
        "DATA_INSUFFICIENT" if not data_sufficient else "PASS" if economics_passed else "FAIL"
    )
    report = {
        "strategy_track": primary.strategy_track.value,
        "economic_model_version": primary.economic_model_version,
        "policy": primary.policy,
        "gate_passed": gate_status == "PASS",
        "gate_status": gate_status,
        "metrics": primary.metrics,
        "score_metrics": score_metrics,
        "data_quality": data_quality,
        "baseline_comparison": baseline_comparison,
        "cost_sources": _cost_source_summary(primary),
    }
    report["markdown"] = render_markdown_report(report)
    return report


def render_markdown_report(report: dict[str, Any]) -> str:
    metrics = report["metrics"]
    return "\n".join(
        [
            "# Leader Swing Gate Report",
            "",
            f"- Gate passed: {report['gate_passed']}",
            f"- Gate status: {report['gate_status']}",
            f"- Net PnL USDC: {metrics['net_pnl_usdc']}",
            f"- Score Net PnL USDC: {report['score_metrics']['net_pnl_usdc']}",
            f"- Sharpe net: {metrics['sharpe_net']}",
            f"- Total trades: {metrics['total_trades']}",
            f"- Economic model: {report['economic_model_version']}",
        ]
    )


def write_gate_report(report: dict[str, Any], output_path: str | Path) -> Path:
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    markdown_path = output.with_suffix(".md")
    markdown_path.write_text(report["markdown"])
    payload = dict(report)
    payload["markdown_path"] = str(markdown_path)
    output.write_text(json.dumps(_jsonable(payload), indent=2, sort_keys=True))
    return output


def _cost_source_summary(run: BacktestRun) -> dict[str, int]:
    counts: dict[str, int] = {}
    for fill in run.fills:
        for source in fill.cost_sources.values():
            counts[source] = counts.get(source, 0) + 1
    return counts


def _spread_sources(fill: BacktestFill) -> list[str]:
    return [
        fill.cost_sources.get("entry_spread", ""),
        fill.cost_sources.get("exit_spread", ""),
    ]


def _has_constant_spread_fallback(fill: BacktestFill) -> bool:
    return any(source.startswith("constant") for source in _spread_sources(fill))


def _has_real_spread_source(fill: BacktestFill) -> bool:
    return all(source in {"orderbook", "candle"} for source in _spread_sources(fill))


def _has_book_spread_source(fill: BacktestFill) -> bool:
    return all(source == "orderbook" for source in _spread_sources(fill))


def _eligible_gate_fills(fills: list[BacktestFill]) -> list[BacktestFill]:
    return [fill for fill in fills if _has_real_spread_source(fill)]


def _data_quality(fills: list[BacktestFill]) -> dict[str, Any]:
    total = Decimal(len(fills))
    if total == 0:
        return {
            "total_fills": 0,
            "real_cost_source_fill_pct": Decimal("0"),
            "book_source_fill_pct": Decimal("0"),
            "constant_fallback_fill_pct": Decimal("0"),
            "score_eligible_fills": 0,
        }
    real_count = sum(1 for fill in fills if _has_real_spread_source(fill))
    book_count = sum(1 for fill in fills if _has_book_spread_source(fill))
    constant_count = sum(1 for fill in fills if _has_constant_spread_fallback(fill))
    return {
        "total_fills": len(fills),
        "real_cost_source_fill_pct": Decimal(real_count) / total,
        "book_source_fill_pct": Decimal(book_count) / total,
        "constant_fallback_fill_pct": Decimal(constant_count) / total,
        "score_eligible_fills": real_count,
    }


def _data_sufficient(data_quality: dict[str, Any]) -> bool:
    return (
        data_quality["real_cost_source_fill_pct"] >= MIN_REAL_COST_SOURCE_PCT
        and data_quality["book_source_fill_pct"] >= MIN_BOOK_SOURCE_PCT
        and data_quality["constant_fallback_fill_pct"] <= MAX_CONSTANT_FALLBACK_PCT
        and data_quality["score_eligible_fills"] > 0
    )
