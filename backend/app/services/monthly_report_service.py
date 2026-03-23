from __future__ import annotations

import calendar
import json
import math
import random
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from statistics import fmean
from typing import Any
from xml.sax.saxutils import escape

from reportlab.graphics.charts.barcharts import VerticalBarChart
from reportlab.graphics.charts.linecharts import HorizontalLineChart
from reportlab.graphics.shapes import Drawing, Rect, String
from reportlab.lib.colors import Color, HexColor
from reportlab.lib.enums import TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, StyleSheet1, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

BACKGROUND = HexColor("#0a0a0f")
PANEL = HexColor("#12151f")
PANEL_ALT = HexColor("#0f1220")
BORDER = HexColor("#1d2435")
GRID = HexColor("#273048")
TEXT_PRIMARY = HexColor("#f5f7ff")
TEXT_MUTED = HexColor("#8f96b2")
ACCENT = HexColor("#00d4aa")
NEGATIVE = HexColor("#ff6b6b")
POSITIVE = HexColor("#55f0c3")
WARNING = HexColor("#ffd166")
WATERMARK = Color(0.15, 0.18, 0.25, alpha=0.13)

MONTH_LOOKUP = {
    "january": 1,
    "janvier": 1,
    "february": 2,
    "fevrier": 2,
    "février": 2,
    "march": 3,
    "mars": 3,
    "april": 4,
    "avril": 4,
    "may": 5,
    "mai": 5,
    "june": 6,
    "juin": 6,
    "july": 7,
    "juillet": 7,
    "august": 8,
    "aout": 8,
    "août": 8,
    "september": 9,
    "septembre": 9,
    "october": 10,
    "octobre": 10,
    "november": 11,
    "novembre": 11,
    "december": 12,
    "decembre": 12,
    "décembre": 12,
}

MARKET_POOL = [
    "Fed cut rates before Q3?",
    "Trump wins Florida primary?",
    "S&P 500 closes above 6,200?",
    "Solana above $250 before April?",
    "Tesla delivers above consensus?",
    "Gold above $2,300 this week?",
    "US CPI surprises below 3%?",
    "Base ETF approved before summer?",
    "Nasdaq ends month green?",
    "Nvidia closes above $1,100?",
    "Oil above $90 before OPEC meeting?",
    "EU approves stablecoin framework?",
    "Bitcoin ETF inflows stay positive?",
    "DOGE above $0.30 before Friday?",
    "ETH staking yield above 4%?",
    "US recession odds below 35%?",
    "Apple launches AI hardware in 2026?",
    "Goldman upgrades Coinbase?",
    "Open interest sets new all-time high?",
    "BTC dominance above 60% this month?",
]


@dataclass(slots=True)
class TradeRow:
    market: str
    strategy: str
    pnl: float
    return_pct: float
    position_size: float
    regime: str

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "TradeRow":
        pnl = _as_float(payload.get("pnl", payload.get("pnl_abs", 0.0)))
        position_size = _as_float(payload.get("position_size", payload.get("notional", 0.0)))
        return_pct = payload.get("return_pct", payload.get("pnl_pct"))
        if return_pct is None:
            return_pct = 0.0 if position_size == 0 else (pnl / position_size) * 100
        return cls(
            market=str(payload.get("market", payload.get("market_title", "Unknown market"))),
            strategy=str(payload.get("strategy", "Latency Arb")),
            pnl=round(pnl, 2),
            return_pct=round(_as_float(return_pct), 2),
            position_size=round(position_size, 2),
            regime=str(payload.get("regime", "Trending")),
        )


@dataclass(slots=True)
class RegimeRow:
    name: str
    share_pct: float
    avg_pnl: float
    win_rate: float
    note: str

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "RegimeRow":
        return cls(
            name=str(payload.get("name", "Unknown")),
            share_pct=round(_as_float(payload.get("share_pct", payload.get("share", 0.0))), 1),
            avg_pnl=round(_as_float(payload.get("avg_pnl", 0.0)), 2),
            win_rate=round(_as_float(payload.get("win_rate", 0.0)), 1),
            note=str(payload.get("note", "")),
        )


@dataclass(frozen=True, slots=True)
class Verdict:
    label: str
    color: Color
    rationale: str


@dataclass(slots=True)
class MonthlyReportPayload:
    period: str
    capital_initial: float
    capital_final: float
    pnl_total: float
    pnl_pct: float
    trade_count: int
    win_rate: float
    sharpe_ratio: float
    max_drawdown_pct: float
    profit_factor: float
    best_trade_label: str
    best_trade_pnl: float
    worst_trade_label: str
    worst_trade_pnl: float
    strategy_usage: dict[str, float]
    bot_name: str = "Poybot"
    equity_curve: list[float] = field(default_factory=list)
    drawdown_curve: list[float] = field(default_factory=list)
    timeline_labels: list[str] = field(default_factory=list)
    trade_pnls: list[float] = field(default_factory=list)
    strategy_performance: dict[str, float] = field(default_factory=dict)
    closed_trades: list[TradeRow] = field(default_factory=list)
    position_sizes: list[float] = field(default_factory=list)
    regime_analysis: list[RegimeRow] = field(default_factory=list)
    confidentiality_label: str = "CONFIDENTIEL"

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "MonthlyReportPayload":
        best_trade = payload.get("best_trade", payload.get("meilleur_trade", {}))
        worst_trade = payload.get("worst_trade", payload.get("pire_trade", {}))
        strategy_usage = payload.get("strategy_usage", payload.get("strategies_utilisees", {})) or {}
        closed_trades = [TradeRow.from_dict(row) for row in payload.get("closed_trades", [])]
        regime_analysis = [RegimeRow.from_dict(row) for row in payload.get("regime_analysis", [])]

        report = cls(
            period=str(payload.get("period", payload.get("periode", "Monthly report"))),
            capital_initial=_as_float(payload.get("capital_initial", 0.0)),
            capital_final=_as_float(payload.get("capital_final", 0.0)),
            pnl_total=_as_float(payload.get("pnl_total", 0.0)),
            pnl_pct=_as_float(payload.get("pnl_pct", 0.0)),
            trade_count=int(payload.get("trade_count", payload.get("nombre_trades", len(closed_trades) or 0))),
            win_rate=_as_float(payload.get("win_rate", 0.0)),
            sharpe_ratio=_as_float(payload.get("sharpe_ratio", 0.0)),
            max_drawdown_pct=-abs(
                _as_float(payload.get("max_drawdown_pct", payload.get("max_drawdown", 0.0)))
            ),
            profit_factor=_as_float(payload.get("profit_factor", 0.0)),
            best_trade_label=_trade_label(best_trade, "Best trade"),
            best_trade_pnl=_trade_pnl(best_trade, payload.get("best_trade_pnl"), positive=True),
            worst_trade_label=_trade_label(worst_trade, "Worst trade"),
            worst_trade_pnl=_trade_pnl(worst_trade, payload.get("worst_trade_pnl"), positive=False),
            strategy_usage={str(key): _as_float(value) for key, value in strategy_usage.items()},
            bot_name=str(payload.get("bot_name", "Poybot")),
            equity_curve=[round(_as_float(value), 2) for value in payload.get("equity_curve", [])],
            drawdown_curve=[round(_as_float(value), 2) for value in payload.get("drawdown_curve", [])],
            timeline_labels=[str(value) for value in payload.get("timeline_labels", [])],
            trade_pnls=[round(_as_float(value), 2) for value in payload.get("trade_pnls", [])],
            strategy_performance={
                str(key): round(_as_float(value), 2)
                for key, value in payload.get("strategy_performance", {}).items()
            },
            closed_trades=closed_trades,
            position_sizes=[round(_as_float(value), 2) for value in payload.get("position_sizes", [])],
            regime_analysis=regime_analysis,
            confidentiality_label=str(payload.get("confidentiality_label", "CONFIDENTIEL")),
        )
        report.hydrate()
        return report

    def hydrate(self) -> None:
        if not self.pnl_total and self.capital_initial and self.capital_final:
            self.pnl_total = round(self.capital_final - self.capital_initial, 2)
        if not self.capital_final and self.capital_initial:
            self.capital_final = round(self.capital_initial + self.pnl_total, 2)
        if not self.pnl_pct and self.capital_initial:
            self.pnl_pct = round((self.pnl_total / self.capital_initial) * 100, 1)
        self.max_drawdown_pct = -abs(self.max_drawdown_pct)

        if self.closed_trades and not self.trade_pnls:
            self.trade_pnls = [round(trade.pnl, 2) for trade in self.closed_trades]
        if not self.trade_count:
            self.trade_count = len(self.closed_trades) or len(self.trade_pnls)

        if not self.strategy_usage:
            self.strategy_usage = _strategy_usage_from_trades(self.closed_trades) or {"Latency Arb": 100.0}
        self.strategy_usage = _normalize_weights(self.strategy_usage)

        if not self.trade_pnls:
            self.trade_pnls = _generate_trade_pnls(self)
        if not self.closed_trades:
            self.closed_trades = _generate_trade_rows(self, self.trade_pnls)
        if not self.position_sizes:
            self.position_sizes = [round(trade.position_size, 2) for trade in self.closed_trades]
        if not self.strategy_performance:
            self.strategy_performance = _aggregate_strategy_performance(self.closed_trades)
        if not self.equity_curve:
            self.equity_curve, self.timeline_labels = _generate_equity_curve(self, self.trade_pnls)
        if not self.timeline_labels:
            self.timeline_labels = _build_timeline_labels(len(self.equity_curve))
        if not self.drawdown_curve:
            self.drawdown_curve = _generate_drawdown_curve(self)
        if not self.regime_analysis:
            self.regime_analysis = _aggregate_regimes(self.closed_trades)

        if not self.best_trade_label:
            self.best_trade_label = max(self.closed_trades, key=lambda trade: trade.pnl).market
        if not self.worst_trade_label:
            self.worst_trade_label = min(self.closed_trades, key=lambda trade: trade.pnl).market
        if not self.best_trade_pnl:
            self.best_trade_pnl = round(max(self.trade_pnls), 2)
        if not self.worst_trade_pnl:
            self.worst_trade_pnl = round(min(self.trade_pnls), 2)


class MonthlyPerformanceReportBuilder:
    def __init__(self, payload: MonthlyReportPayload) -> None:
        self.payload = payload
        self.styles = _build_styles()
        self.page_width, self.page_height = A4
        self.left_margin = 14 * mm
        self.right_margin = 14 * mm
        self.top_margin = 12 * mm
        self.bottom_margin = 10 * mm
        self.content_width = self.page_width - self.left_margin - self.right_margin
        self.half_width = (self.content_width - 10) / 2

    def build(self, output_path: str | Path) -> Path:
        destination = Path(output_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        doc = SimpleDocTemplate(
            str(destination),
            pagesize=A4,
            leftMargin=self.left_margin,
            rightMargin=self.right_margin,
            topMargin=self.top_margin,
            bottomMargin=self.bottom_margin,
        )
        doc.build(
            self._story(),
            onFirstPage=self._draw_page_chrome,
            onLaterPages=self._draw_page_chrome,
        )
        return destination

    def _story(self) -> list[Any]:
        story: list[Any] = []
        story.extend(self._page_one())
        story.append(PageBreak())
        story.extend(self._page_two())
        story.append(PageBreak())
        story.extend(self._page_three())
        story.append(PageBreak())
        story.extend(self._page_four())
        return story

    def _page_one(self) -> list[Any]:
        verdict = performance_verdict(self.payload.sharpe_ratio)
        metrics_grid = Table(
            [
                [
                    self._metric_card("Capital initial", f"{self.payload.capital_initial:,.0f} USDC"),
                    self._metric_card("Capital final", f"{self.payload.capital_final:,.0f} USDC"),
                    self._metric_card(
                        "PnL total",
                        f"{self.payload.pnl_total:+,.0f} USDC",
                        value_color=ACCENT if self.payload.pnl_total >= 0 else NEGATIVE,
                    ),
                    self._metric_card(
                        "Rendement",
                        f"{self.payload.pnl_pct:+.1f}%",
                        value_color=ACCENT if self.payload.pnl_pct >= 0 else NEGATIVE,
                    ),
                ],
                [
                    self._metric_card("Nombre de trades", f"{self.payload.trade_count}"),
                    self._metric_card("Win rate", f"{self.payload.win_rate:.1f}%"),
                    self._metric_card("Sharpe ratio", f"{self.payload.sharpe_ratio:.2f}"),
                    self._metric_card(
                        "Max drawdown",
                        f"{self.payload.max_drawdown_pct:.1f}%",
                        value_color=NEGATIVE,
                    ),
                ],
                [
                    self._metric_card("Profit factor", f"{self.payload.profit_factor:.2f}"),
                    self._metric_card("Meilleur trade", f"{self.payload.best_trade_pnl:+.2f} USDC"),
                    self._metric_card(
                        "Pire trade",
                        f"{self.payload.worst_trade_pnl:+.2f} USDC",
                        value_color=NEGATIVE,
                    ),
                    self._metric_card("Stratégies actives", f"{len(self.payload.strategy_usage)}"),
                ],
            ],
            colWidths=[self.content_width / 4] * 4,
            style=TableStyle(
                [
                    ("LEFTPADDING", (0, 0), (-1, -1), 0),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                    ("TOPPADDING", (0, 0), (-1, -1), 0),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
                ]
            ),
        )

        trade_summary = Table(
            [
                [
                    self._compact_trade_card(
                        "Meilleur trade",
                        self.payload.best_trade_label,
                        self.payload.best_trade_pnl,
                        POSITIVE,
                    ),
                    self._compact_trade_card(
                        "Pire trade",
                        self.payload.worst_trade_label,
                        self.payload.worst_trade_pnl,
                        NEGATIVE,
                    ),
                ]
            ],
            colWidths=[(self.content_width - 10) / 2] * 2,
            style=TableStyle(
                [
                    ("LEFTPADDING", (0, 0), (-1, -1), 0),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                    ("TOPPADDING", (0, 0), (-1, -1), 0),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
                ]
            ),
        )

        verdict_panel = self._panel(
            [
                Paragraph("Verdict de performance", self.styles["section_kicker"]),
                Spacer(1, 6),
                Table(
                    [
                        [
                            [
                                Paragraph(verdict.label, self._inline_style("verdict", verdict.color, 26)),
                                Paragraph(verdict.rationale, self.styles["body"]),
                            ],
                            [
                                Paragraph("Barème de lecture", self.styles["small_label"]),
                                Paragraph(
                                    "Sharpe &gt; 2.0 = Excellent, &gt;= 1.2 = Bon, &gt;= 0.5 = Passable",
                                    self.styles["body"],
                                ),
                                Spacer(1, 6),
                                Paragraph("Allocation stratégies", self.styles["small_label"]),
                                Spacer(1, 6),
                                self._strategy_badges(),
                            ],
                        ]
                    ],
                    colWidths=[160, self.content_width - 200],
                    style=TableStyle(
                        [
                            ("VALIGN", (0, 0), (-1, -1), "TOP"),
                            ("LEFTPADDING", (0, 0), (-1, -1), 0),
                            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                            ("TOPPADDING", (0, 0), (-1, -1), 0),
                            ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
                        ]
                    ),
                ),
            ]
        )

        return [
            self._header_panel(),
            Spacer(1, 10),
            Paragraph("Executive Summary", self.styles["page_title"]),
            Paragraph(
                f"Rapport mensuel de performance Polymarket | {escape(self.payload.period)}",
                self.styles["subtitle"],
            ),
            Spacer(1, 12),
            metrics_grid,
            Spacer(1, 12),
            verdict_panel,
            Spacer(1, 12),
            trade_summary,
        ]

    def _page_two(self) -> list[Any]:
        equity_chart = self._chart_panel(
            "Equity curve",
            self._line_chart(
                self.payload.equity_curve,
                self.payload.timeline_labels,
                width=self.content_width - 24,
                height=175,
                stroke_color=ACCENT,
                formatter="currency",
            ),
            subtitle="Evolution quotidienne du capital total sur la période.",
        )
        pnl_histogram = self._chart_panel(
            "Distribution des PnL",
            self._histogram_chart(
                self.payload.trade_pnls,
                width=self.half_width - 24,
                height=145,
                formatter="count",
            ),
            width=self.half_width,
            subtitle="Répartition des gains et pertes par trade.",
        )
        strategy_chart = self._chart_panel(
            "Performance par stratégie",
            self._strategy_bar_chart(width=self.half_width - 24, height=145),
            width=self.half_width,
            subtitle="Contribution nette au PnL mensuel.",
        )

        charts_row = Table(
            [[pnl_histogram, strategy_chart]],
            colWidths=[self.half_width, self.half_width],
            style=TableStyle(
                [
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 0),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                    ("TOPPADDING", (0, 0), (-1, -1), 0),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
                ]
            ),
        )

        return [
            self._section_header(
                "Performance détaillée",
                "Lecture quantitative de la trajectoire de capital, de la dispersion des résultats et de la contribution des stratégies.",
            ),
            equity_chart,
            Spacer(1, 12),
            charts_row,
        ]

    def _page_three(self) -> list[Any]:
        winners = sorted(self.payload.closed_trades, key=lambda trade: trade.pnl, reverse=True)[:5]
        losers = sorted(self.payload.closed_trades, key=lambda trade: trade.pnl)[:5]
        return [
            self._section_header(
                "Top trades",
                "Les meilleurs et les pires trades du mois pour comprendre où la performance a été créée et détruite.",
            ),
            self._trades_table("Top 5 meilleurs trades", winners, POSITIVE),
            Spacer(1, 12),
            self._trades_table("Top 5 pires trades", losers, NEGATIVE),
        ]

    def _page_four(self) -> list[Any]:
        drawdown_chart = self._chart_panel(
            "Drawdown timeline",
            self._line_chart(
                self.payload.drawdown_curve,
                self.payload.timeline_labels,
                width=self.content_width - 24,
                height=165,
                stroke_color=NEGATIVE,
                formatter="percent",
            ),
            subtitle=f"Creux mensuel maximum observé: {self.payload.max_drawdown_pct:.1f}%.",
        )
        position_chart = self._chart_panel(
            "Tailles de positions",
            self._histogram_chart(
                self.payload.position_sizes,
                width=self.half_width - 24,
                height=138,
                formatter="count",
                label_formatter="size",
            ),
            width=self.half_width,
            subtitle="Distribution notionnelle par trade (USDC).",
        )
        regime_chart = self._chart_panel(
            "Régimes de marché",
            self._regime_bar_chart(width=self.half_width - 24, height=138),
            width=self.half_width,
            subtitle="PnL moyen par trade selon le régime dominant.",
        )
        bottom_row = Table(
            [[position_chart, regime_chart]],
            colWidths=[self.half_width, self.half_width],
            style=TableStyle(
                [
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 0),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                    ("TOPPADDING", (0, 0), (-1, -1), 0),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
                ]
            ),
        )
        return [
            self._section_header(
                "Risk analysis",
                "Vue consolidée du risque de parcours, du sizing et des performances selon le contexte de marché.",
            ),
            drawdown_chart,
            Spacer(1, 12),
            bottom_row,
            Spacer(1, 12),
            self._risk_notes_panel(),
        ]

    def _header_panel(self) -> Table:
        return Table(
            [
                [
                    self._logo_drawing(),
                    [
                        Paragraph(self.payload.bot_name, self.styles["brand"]),
                        Paragraph("Polymarket Monthly Performance Report", self.styles["subtitle"]),
                    ],
                    [
                        Paragraph("Période", self.styles["small_label_right"]),
                        Paragraph(escape(self.payload.period), self.styles["period_value"]),
                    ],
                ]
            ],
            colWidths=[92, self.content_width - 194, 102],
            style=TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, -1), PANEL),
                    ("BOX", (0, 0), (-1, -1), 0.7, BORDER),
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 14),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 14),
                    ("TOPPADDING", (0, 0), (-1, -1), 12),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 12),
                ]
            ),
        )

    def _section_header(self, title: str, subtitle: str) -> Table:
        return self._panel(
            [
                Paragraph(title, self.styles["page_title"]),
                Spacer(1, 4),
                Paragraph(escape(subtitle), self.styles["subtitle"]),
            ]
        )

    def _metric_card(self, label: str, value: str, value_color: Color = TEXT_PRIMARY) -> Table:
        width = self.content_width / 4 - 6
        return Table(
            [
                [
                    Paragraph(escape(label), self.styles["metric_label"]),
                    Paragraph(escape(value), self._inline_style("metric_value", value_color, 16)),
                ]
            ],
            colWidths=[width],
            style=TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, -1), PANEL_ALT),
                    ("BOX", (0, 0), (-1, -1), 0.7, BORDER),
                    ("LEFTPADDING", (0, 0), (-1, -1), 10),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                    ("TOPPADDING", (0, 0), (-1, -1), 8),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
                ]
            ),
        )

    def _compact_trade_card(self, label: str, market: str, pnl: float, color: Color) -> Table:
        return self._panel(
            [
                Paragraph(escape(label), self.styles["section_kicker"]),
                Spacer(1, 4),
                Paragraph(escape(_truncate(market, 52)), self.styles["body"]),
                Spacer(1, 6),
                Paragraph(f"{pnl:+.2f} USDC", self._inline_style("compact_trade", color, 18)),
            ],
            width=(self.content_width - 10) / 2,
        )

    def _panel(self, content: Any, width: float | None = None, background: Color = PANEL) -> Table:
        return Table(
            [[content]],
            colWidths=[width or self.content_width],
            style=TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, -1), background),
                    ("BOX", (0, 0), (-1, -1), 0.7, BORDER),
                    ("LEFTPADDING", (0, 0), (-1, -1), 12),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 12),
                    ("TOPPADDING", (0, 0), (-1, -1), 12),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 12),
                ]
            ),
        )

    def _chart_panel(self, title: str, drawing: Drawing, subtitle: str, width: float | None = None) -> Table:
        return self._panel(
            [
                Paragraph(escape(title), self.styles["section_kicker"]),
                Spacer(1, 2),
                Paragraph(escape(subtitle), self.styles["body"]),
                Spacer(1, 8),
                drawing,
            ],
            width=width,
        )

    def _strategy_badges(self) -> Table:
        weights = list(self.payload.strategy_usage.items())
        widths = [max(92, (self.content_width - 220) / max(len(weights), 1)) for _ in weights]
        row: list[Any] = []
        for index, (name, percentage) in enumerate(weights):
            accent = ACCENT if index % 2 == 0 else WARNING
            row.append(
                Table(
                    [
                        [
                            Paragraph(escape(_truncate(name, 24)), self.styles["badge_label"]),
                            Paragraph(f"{percentage:.0f}%", self._inline_style("badge_value", accent, 14)),
                        ]
                    ],
                    colWidths=[widths[index] - 8],
                    style=TableStyle(
                        [
                            ("BACKGROUND", (0, 0), (-1, -1), PANEL_ALT),
                            ("BOX", (0, 0), (-1, -1), 0.6, accent),
                            ("LEFTPADDING", (0, 0), (-1, -1), 8),
                            ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                            ("TOPPADDING", (0, 0), (-1, -1), 6),
                            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                        ]
                    ),
                )
            )
        return Table(
            [row],
            colWidths=widths,
            style=TableStyle(
                [
                    ("LEFTPADDING", (0, 0), (-1, -1), 0),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                    ("TOPPADDING", (0, 0), (-1, -1), 0),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
                ]
            ),
        )

    def _trades_table(self, title: str, trades: list[TradeRow], accent_color: Color) -> Table:
        table_rows: list[list[Any]] = [
            [
                Paragraph("Rang", self.styles["table_header"]),
                Paragraph("Trade", self.styles["table_header"]),
                Paragraph("Stratégie", self.styles["table_header"]),
                Paragraph("Taille", self.styles["table_header"]),
                Paragraph("PnL", self.styles["table_header_right"]),
            ]
        ]
        pnl_colors: dict[int, Color] = {}
        for rank, trade in enumerate(trades, start=1):
            row_index = len(table_rows)
            pnl_colors[row_index] = POSITIVE if trade.pnl >= 0 else NEGATIVE
            table_rows.append(
                [
                    Paragraph(str(rank), self.styles["table_cell"]),
                    Paragraph(escape(_truncate(trade.market, 44)), self.styles["table_cell"]),
                    Paragraph(escape(trade.strategy), self.styles["table_cell"]),
                    Paragraph(f"{trade.position_size:,.0f} USDC", self.styles["table_cell"]),
                    Paragraph(f"{trade.pnl:+.2f}", self.styles["table_cell_right"]),
                ]
            )
        table = Table(table_rows, colWidths=[34, 250, 88, 82, 60], repeatRows=1)
        style = [
            ("BACKGROUND", (0, 0), (-1, 0), accent_color),
            ("TEXTCOLOR", (0, 0), (-1, 0), BACKGROUND),
            ("BACKGROUND", (0, 1), (-1, -1), PANEL),
            ("BOX", (0, 0), (-1, -1), 0.7, BORDER),
            ("INNERGRID", (0, 0), (-1, -1), 0.4, BORDER),
            ("LEFTPADDING", (0, 0), (-1, -1), 8),
            ("RIGHTPADDING", (0, 0), (-1, -1), 8),
            ("TOPPADDING", (0, 0), (-1, -1), 8),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ]
        for row_index in range(1, len(table_rows)):
            if row_index % 2 == 0:
                style.append(("BACKGROUND", (0, row_index), (-1, row_index), PANEL_ALT))
            style.append(("TEXTCOLOR", (-1, row_index), (-1, row_index), pnl_colors[row_index]))
        table.setStyle(TableStyle(style))
        return self._panel(
            [
                Paragraph(escape(title), self.styles["section_kicker"]),
                Spacer(1, 8),
                table,
            ]
        )

    def _risk_notes_panel(self) -> Table:
        dominant_strategy, dominant_weight = max(
            self.payload.strategy_usage.items(), key=lambda item: item[1]
        )
        best_regime = max(self.payload.regime_analysis, key=lambda item: item.avg_pnl)
        weakest_regime = min(self.payload.regime_analysis, key=lambda item: item.avg_pnl)
        notes = [
            f"Risque de parcours maîtrisé avec un drawdown maximal limité à {self.payload.max_drawdown_pct:.1f}% sur le mois.",
            f"Concentration opérationnelle élevée sur {dominant_strategy} ({dominant_weight:.0f}% de l'activité), à surveiller si la liquidité se compresse.",
            f"Le régime le plus favorable a été {best_regime.name.lower()} ({best_regime.avg_pnl:+.2f} USDC/trade), tandis que {weakest_regime.name.lower()} appelle davantage de prudence.",
        ]
        flowables: list[Any] = [Paragraph("Constats de risque", self.styles["section_kicker"]), Spacer(1, 6)]
        for note in notes:
            flowables.append(Paragraph(f"- {escape(note)}", self.styles["body"]))
            flowables.append(Spacer(1, 4))
        return self._panel(flowables)

    def _logo_drawing(self) -> Drawing:
        drawing = Drawing(76, 30)
        drawing.add(Rect(0, 0, 26, 26, rx=6, ry=6, fillColor=ACCENT, strokeColor=ACCENT))
        drawing.add(String(8, 7, "P", fontName="Helvetica-Bold", fontSize=15, fillColor=BACKGROUND))
        drawing.add(Rect(34, 8, 36, 2.5, fillColor=TEXT_PRIMARY, strokeColor=TEXT_PRIMARY))
        drawing.add(Rect(34, 15, 24, 2.5, fillColor=WARNING, strokeColor=WARNING))
        return drawing

    def _line_chart(
        self,
        series: list[float],
        labels: list[str],
        width: float,
        height: float,
        stroke_color: Color,
        formatter: str,
    ) -> Drawing:
        drawing = Drawing(width, height)
        drawing.add(Rect(0, 0, width, height, fillColor=PANEL, strokeColor=PANEL))
        chart = HorizontalLineChart()
        chart.x = 36
        chart.y = 26
        chart.width = width - 54
        chart.height = height - 42
        chart.data = [series]
        chart.joinedLines = 1
        chart.lines[0].strokeColor = stroke_color
        chart.lines[0].strokeWidth = 2.4
        chart.categoryAxis.categoryNames = labels
        chart.categoryAxis.strokeColor = BORDER
        chart.categoryAxis.labels.fillColor = TEXT_MUTED
        chart.categoryAxis.labels.fontName = "Helvetica"
        chart.categoryAxis.labels.fontSize = 7
        chart.categoryAxis.tickDown = 3
        chart.valueAxis.strokeColor = BORDER
        chart.valueAxis.visibleGrid = True
        chart.valueAxis.gridStrokeColor = GRID
        chart.valueAxis.gridStrokeWidth = 0.35
        chart.valueAxis.labels.fillColor = TEXT_MUTED
        chart.valueAxis.labels.fontName = "Helvetica"
        chart.valueAxis.labels.fontSize = 7
        chart.valueAxis.labelTextFormat = _axis_formatter(formatter)
        value_min = min(series)
        value_max = max(series)
        padding = max((value_max - value_min) * 0.12, 1.0)
        chart.valueAxis.valueMin = math.floor(value_min - padding)
        chart.valueAxis.valueMax = math.ceil(value_max + padding)
        drawing.add(chart)
        return drawing

    def _histogram_chart(
        self,
        values: list[float],
        width: float,
        height: float,
        formatter: str,
        label_formatter: str = "range",
    ) -> Drawing:
        labels, counts = _bucketize(values, bucket_count=7, label_formatter=label_formatter)
        return self._vertical_bar_chart(
            labels=labels,
            values=counts,
            width=width,
            height=height,
            formatter=formatter,
            color_sequence=[ACCENT for _ in counts],
        )

    def _strategy_bar_chart(self, width: float, height: float) -> Drawing:
        labels = list(self.payload.strategy_performance.keys())
        values = [round(value, 2) for value in self.payload.strategy_performance.values()]
        palette = [ACCENT, WARNING, POSITIVE, HexColor("#7aa2ff")]
        colors_for_bars = [palette[index % len(palette)] for index in range(len(values))]
        return self._vertical_bar_chart(
            labels=labels,
            values=values,
            width=width,
            height=height,
            formatter="currency",
            color_sequence=colors_for_bars,
        )

    def _regime_bar_chart(self, width: float, height: float) -> Drawing:
        labels = [row.name for row in self.payload.regime_analysis]
        values = [round(row.avg_pnl, 2) for row in self.payload.regime_analysis]
        colors_for_bars = [POSITIVE if value >= 0 else NEGATIVE for value in values]
        return self._vertical_bar_chart(
            labels=labels,
            values=values,
            width=width,
            height=height,
            formatter="currency",
            color_sequence=colors_for_bars,
        )

    def _vertical_bar_chart(
        self,
        labels: list[str],
        values: list[float],
        width: float,
        height: float,
        formatter: str,
        color_sequence: list[Color],
    ) -> Drawing:
        drawing = Drawing(width, height)
        drawing.add(Rect(0, 0, width, height, fillColor=PANEL, strokeColor=PANEL))
        chart = VerticalBarChart()
        chart.x = 36
        chart.y = 28
        chart.width = width - 52
        chart.height = height - 46
        chart.data = [values]
        chart.categoryAxis.categoryNames = [_truncate(label, 12) for label in labels]
        chart.categoryAxis.strokeColor = BORDER
        chart.categoryAxis.labels.fillColor = TEXT_MUTED
        chart.categoryAxis.labels.fontName = "Helvetica"
        chart.categoryAxis.labels.fontSize = 7
        chart.categoryAxis.labels.angle = 20
        chart.categoryAxis.labels.dy = -6
        chart.valueAxis.strokeColor = BORDER
        chart.valueAxis.visibleGrid = True
        chart.valueAxis.gridStrokeColor = GRID
        chart.valueAxis.gridStrokeWidth = 0.35
        chart.valueAxis.labels.fillColor = TEXT_MUTED
        chart.valueAxis.labels.fontName = "Helvetica"
        chart.valueAxis.labels.fontSize = 7
        chart.valueAxis.labelTextFormat = _axis_formatter(formatter)
        chart.barLabels.fillColor = TEXT_PRIMARY
        chart.barLabels.fontName = "Helvetica-Bold"
        chart.barLabels.fontSize = 7
        chart.barLabels.nudge = 7
        chart.barLabels.boxAnchor = "s"
        chart.barLabels.visible = True
        chart.barLabelFormat = _axis_formatter(formatter, compact=True)
        lower = min(0.0, min(values))
        upper = max(0.0, max(values))
        spread = max(upper - lower, 1.0)
        chart.valueAxis.valueMin = math.floor(lower - spread * 0.12)
        chart.valueAxis.valueMax = math.ceil(upper + spread * 0.18)
        for index, color in enumerate(color_sequence):
            chart.bars[(0, index)].fillColor = color
            chart.bars[(0, index)].strokeColor = color
        drawing.add(chart)
        return drawing

    def _draw_page_chrome(self, canvas, doc) -> None:  # type: ignore[no-untyped-def]
        canvas.saveState()
        canvas.setFillColor(BACKGROUND)
        canvas.rect(0, 0, self.page_width, self.page_height, stroke=0, fill=1)
        canvas.setFillColor(WATERMARK)
        canvas.setFont("Helvetica-Bold", 54)
        canvas.translate(self.page_width / 2, self.page_height / 2)
        canvas.rotate(37)
        canvas.drawCentredString(0, 0, self.payload.confidentiality_label)
        canvas.rotate(-37)
        canvas.translate(-self.page_width / 2, -self.page_height / 2)
        canvas.setStrokeColor(BORDER)
        canvas.setLineWidth(0.7)
        canvas.line(
            self.left_margin,
            self.page_height - 22,
            self.page_width - self.right_margin,
            self.page_height - 22,
        )
        canvas.line(self.left_margin, 18, self.page_width - self.right_margin, 18)
        canvas.setFillColor(TEXT_MUTED)
        canvas.setFont("Helvetica", 8)
        canvas.drawString(self.left_margin, 8, self.payload.bot_name)
        canvas.drawRightString(
            self.page_width - self.right_margin,
            8,
            f"Page {canvas.getPageNumber()}",
        )
        canvas.restoreState()

    def _inline_style(self, parent: str, color: Color, size: int) -> ParagraphStyle:
        base = self.styles[parent]
        return ParagraphStyle(
            name=f"{parent}_{size}_{color}",
            parent=base,
            textColor=color,
            fontSize=size,
        )


def performance_verdict(sharpe_ratio: float) -> Verdict:
    if sharpe_ratio > 2.0:
        return Verdict(
            label="Excellent",
            color=ACCENT,
            rationale="Rendement ajusté du risque de premier plan avec un Sharpe supérieur à 2.0.",
        )
    if sharpe_ratio >= 1.2:
        return Verdict(
            label="Bon",
            color=WARNING,
            rationale="Profil sain et robuste, mais encore en dessous du palier d'excellence.",
        )
    if sharpe_ratio >= 0.5:
        return Verdict(
            label="Passable",
            color=HexColor("#f4a261"),
            rationale="La performance reste positive mais le couple rendement/risque manque de constance.",
        )
    return Verdict(
        label="Mauvais",
        color=NEGATIVE,
        rationale="Le rendement ajusté du risque est insuffisant et appelle des ajustements de stratégie.",
    )


def example_monthly_payload() -> MonthlyReportPayload:
    payload = MonthlyReportPayload(
        period="Mars 2026",
        capital_initial=1_000.0,
        capital_final=1_387.0,
        pnl_total=387.0,
        pnl_pct=38.7,
        trade_count=142,
        win_rate=72.4,
        sharpe_ratio=2.41,
        max_drawdown_pct=-6.8,
        profit_factor=3.12,
        best_trade_label="BTC above $95k",
        best_trade_pnl=42.10,
        worst_trade_label="ETH above $3k",
        worst_trade_pnl=-18.30,
        strategy_usage={"Latency Arb": 82.0, "Spread Arb": 18.0},
    )
    payload.hydrate()
    return payload


def load_monthly_report_payload(source: str | Path) -> MonthlyReportPayload:
    raw = json.loads(Path(source).read_text(encoding="utf-8"))
    return MonthlyReportPayload.from_dict(raw)


def generate_monthly_report_pdf(payload: MonthlyReportPayload, output_path: str | Path) -> Path:
    payload.hydrate()
    builder = MonthlyPerformanceReportBuilder(payload)
    return builder.build(output_path)


def _build_styles() -> StyleSheet1:
    styles = getSampleStyleSheet()
    styles.add(
        ParagraphStyle(
            name="page_title",
            parent=styles["Heading1"],
            fontName="Helvetica-Bold",
            fontSize=20,
            leading=24,
            textColor=TEXT_PRIMARY,
            spaceAfter=0,
        )
    )
    styles.add(
        ParagraphStyle(
            name="brand",
            parent=styles["Heading2"],
            fontName="Helvetica-Bold",
            fontSize=18,
            leading=22,
            textColor=TEXT_PRIMARY,
            spaceAfter=2,
        )
    )
    styles.add(
        ParagraphStyle(
            name="subtitle",
            parent=styles["BodyText"],
            fontName="Helvetica",
            fontSize=9.5,
            leading=13,
            textColor=TEXT_MUTED,
            spaceAfter=0,
        )
    )
    styles.add(
        ParagraphStyle(
            name="section_kicker",
            parent=styles["BodyText"],
            fontName="Helvetica-Bold",
            fontSize=10,
            leading=12,
            textColor=TEXT_PRIMARY,
            spaceAfter=0,
        )
    )
    styles.add(
        ParagraphStyle(
            name="body",
            parent=styles["BodyText"],
            fontName="Helvetica",
            fontSize=9,
            leading=13,
            textColor=TEXT_MUTED,
            spaceAfter=0,
        )
    )
    styles.add(
        ParagraphStyle(
            name="metric_label",
            parent=styles["BodyText"],
            fontName="Helvetica",
            fontSize=8,
            leading=10,
            textColor=TEXT_MUTED,
            spaceAfter=4,
        )
    )
    styles.add(
        ParagraphStyle(
            name="metric_value",
            parent=styles["BodyText"],
            fontName="Helvetica-Bold",
            fontSize=15,
            leading=18,
            textColor=TEXT_PRIMARY,
            spaceAfter=0,
        )
    )
    styles.add(
        ParagraphStyle(
            name="verdict",
            parent=styles["BodyText"],
            fontName="Helvetica-Bold",
            fontSize=24,
            leading=28,
            textColor=ACCENT,
            spaceAfter=0,
        )
    )
    styles.add(
        ParagraphStyle(
            name="compact_trade",
            parent=styles["BodyText"],
            fontName="Helvetica-Bold",
            fontSize=18,
            leading=22,
            textColor=TEXT_PRIMARY,
            spaceAfter=0,
        )
    )
    styles.add(
        ParagraphStyle(
            name="badge_label",
            parent=styles["BodyText"],
            fontName="Helvetica",
            fontSize=8,
            leading=10,
            textColor=TEXT_MUTED,
            spaceAfter=2,
        )
    )
    styles.add(
        ParagraphStyle(
            name="badge_value",
            parent=styles["BodyText"],
            fontName="Helvetica-Bold",
            fontSize=12,
            leading=14,
            textColor=TEXT_PRIMARY,
            spaceAfter=0,
        )
    )
    styles.add(
        ParagraphStyle(
            name="small_label",
            parent=styles["BodyText"],
            fontName="Helvetica-Bold",
            fontSize=8,
            leading=10,
            textColor=TEXT_PRIMARY,
            spaceAfter=0,
        )
    )
    styles.add(
        ParagraphStyle(
            name="small_label_right",
            parent=styles["small_label"],
            alignment=TA_RIGHT,
        )
    )
    styles.add(
        ParagraphStyle(
            name="period_value",
            parent=styles["BodyText"],
            fontName="Helvetica-Bold",
            fontSize=12,
            leading=16,
            alignment=TA_RIGHT,
            textColor=TEXT_PRIMARY,
            spaceAfter=0,
        )
    )
    styles.add(
        ParagraphStyle(
            name="table_header",
            parent=styles["BodyText"],
            fontName="Helvetica-Bold",
            fontSize=8,
            leading=10,
            textColor=BACKGROUND,
            alignment=TA_LEFT,
            spaceAfter=0,
        )
    )
    styles.add(
        ParagraphStyle(
            name="table_header_right",
            parent=styles["table_header"],
            alignment=TA_RIGHT,
        )
    )
    styles.add(
        ParagraphStyle(
            name="table_cell",
            parent=styles["BodyText"],
            fontName="Helvetica",
            fontSize=8.5,
            leading=11,
            textColor=TEXT_PRIMARY,
            alignment=TA_LEFT,
            spaceAfter=0,
        )
    )
    styles.add(
        ParagraphStyle(
            name="table_cell_right",
            parent=styles["table_cell"],
            alignment=TA_RIGHT,
        )
    )
    return styles


def _as_float(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        clean = value.replace("USDC", "").replace("%", "").replace(",", "").strip()
        if not clean:
            return 0.0
        return float(clean)
    return float(value)


def _trade_label(value: Any, fallback: str) -> str:
    if isinstance(value, dict):
        return str(value.get("label", value.get("market", value.get("market_title", ""))))
    if isinstance(value, str):
        return value
    return fallback


def _trade_pnl(value: Any, fallback: Any, positive: bool) -> float:
    source = fallback
    if isinstance(value, dict):
        source = value.get("pnl", value.get("value", fallback))
    amount = _as_float(source)
    return abs(round(amount, 2)) if positive else -abs(round(amount, 2))


def _normalize_weights(values: dict[str, float]) -> dict[str, float]:
    if not values:
        return {}
    total = sum(abs(value) for value in values.values())
    if total == 0:
        return {key: 0.0 for key in values}
    return {key: round((abs(value) / total) * 100, 1) for key, value in values.items()}


def _strategy_usage_from_trades(trades: list[TradeRow]) -> dict[str, float]:
    if not trades:
        return {}
    counts: defaultdict[str, int] = defaultdict(int)
    for trade in trades:
        counts[trade.strategy] += 1
    total = sum(counts.values())
    return {key: round((value / total) * 100, 1) for key, value in counts.items()}


def _aggregate_strategy_performance(trades: list[TradeRow]) -> dict[str, float]:
    performance: defaultdict[str, float] = defaultdict(float)
    for trade in trades:
        performance[trade.strategy] += trade.pnl
    ordered = sorted(performance.items(), key=lambda item: item[1], reverse=True)
    return {name: round(value, 2) for name, value in ordered}


def _generate_trade_pnls(payload: MonthlyReportPayload) -> list[float]:
    rng = random.Random(202603)
    trade_count = max(payload.trade_count, 10)
    winners = min(trade_count - 1, max(1, round(trade_count * (payload.win_rate / 100))))
    losers = max(1, trade_count - winners)

    gross_loss_abs = abs(payload.pnl_total) / max(abs(payload.profit_factor - 1.0), 0.25)
    gross_loss_abs = max(
        gross_loss_abs,
        abs(payload.worst_trade_pnl) + max(0, losers - 1) * 1.2,
    )
    gross_profit = max(
        gross_loss_abs + payload.pnl_total,
        abs(payload.best_trade_pnl) + max(0, winners - 1) * 0.9,
    )
    gross_loss_abs = round(gross_profit - payload.pnl_total, 2)
    gross_profit = round(gross_profit, 2)

    win_values = _partition_total(
        total=gross_profit,
        count=winners,
        anchor=max(abs(payload.best_trade_pnl), 1.0),
        rng=rng,
    )
    loss_values = _partition_total(
        total=gross_loss_abs,
        count=losers,
        anchor=max(abs(payload.worst_trade_pnl), 0.8),
        rng=rng,
    )
    pnls = [round(value, 2) for value in win_values] + [round(-value, 2) for value in loss_values]
    rng.shuffle(pnls)
    max_index = pnls.index(max(pnls))
    min_index = pnls.index(min(pnls))
    pnls[max_index] = round(payload.best_trade_pnl, 2)
    pnls[min_index] = round(payload.worst_trade_pnl, 2)
    adjustment = round(payload.pnl_total - sum(pnls), 2)
    if adjustment:
        for index, pnl in enumerate(pnls):
            if index in {max_index, min_index}:
                continue
            pnls[index] = round(pnl + adjustment, 2)
            break
    return [round(value, 2) for value in pnls]


def _partition_total(total: float, count: int, anchor: float, rng: random.Random) -> list[float]:
    if count <= 0:
        return []
    if count == 1:
        return [round(total, 2)]
    minimum_slice = max(0.05, min(0.25, total / max(count * 2, 1)))
    anchor = max(minimum_slice, min(anchor, round(total - ((count - 1) * minimum_slice), 2)))
    remainder = round(total - anchor, 2)
    weights = [rng.uniform(0.7, 1.3) for _ in range(count - 1)]
    scale = remainder / sum(weights)
    values = [round(max(minimum_slice, weight * scale), 2) for weight in weights]
    difference = round(total - anchor - sum(values), 2)
    values[-1] = round(values[-1] + difference, 2)
    return [round(anchor, 2), *values]


def _generate_trade_rows(payload: MonthlyReportPayload, trade_pnls: list[float]) -> list[TradeRow]:
    rng = random.Random(202604)
    strategy_counts = _allocate_counts(payload.trade_count, payload.strategy_usage)
    strategy_pool: list[str] = []
    for name, count in strategy_counts.items():
        strategy_pool.extend([name] * count)
    while len(strategy_pool) < len(trade_pnls):
        strategy_pool.append(next(iter(payload.strategy_usage)))
    rng.shuffle(strategy_pool)

    regime_pool = _expand_weighted_pool(
        total=len(trade_pnls),
        weights={"Trending": 46.0, "Range": 34.0, "Event-driven": 20.0},
    )
    rng.shuffle(regime_pool)

    trades: list[TradeRow] = []
    for index, pnl in enumerate(trade_pnls):
        target_return = rng.uniform(3.5, 12.5)
        position_size = max(35.0, min(420.0, abs(pnl) * 100 / target_return))
        return_pct = 0.0 if position_size == 0 else (pnl / position_size) * 100
        market = MARKET_POOL[index % len(MARKET_POOL)]
        trades.append(
            TradeRow(
                market=market,
                strategy=strategy_pool[index],
                pnl=round(pnl, 2),
                return_pct=round(return_pct, 2),
                position_size=round(position_size, 2),
                regime=regime_pool[index],
            )
        )

    best_index = max(range(len(trades)), key=lambda idx: trades[idx].pnl)
    worst_index = min(range(len(trades)), key=lambda idx: trades[idx].pnl)
    trades[best_index].market = payload.best_trade_label
    trades[best_index].pnl = round(payload.best_trade_pnl, 2)
    trades[best_index].return_pct = round(
        (trades[best_index].pnl / trades[best_index].position_size) * 100, 2
    )
    trades[worst_index].market = payload.worst_trade_label
    trades[worst_index].pnl = round(payload.worst_trade_pnl, 2)
    trades[worst_index].return_pct = round(
        (trades[worst_index].pnl / trades[worst_index].position_size) * 100, 2
    )
    return trades


def _allocate_counts(total: int, weights: dict[str, float]) -> dict[str, int]:
    raw_counts = {key: (total * value) / 100 for key, value in weights.items()}
    counts = {key: math.floor(value) for key, value in raw_counts.items()}
    remaining = total - sum(counts.values())
    order = sorted(raw_counts.items(), key=lambda item: item[1] - counts[item[0]], reverse=True)
    for index in range(remaining):
        counts[order[index % len(order)][0]] += 1
    return counts


def _expand_weighted_pool(total: int, weights: dict[str, float]) -> list[str]:
    counts = _allocate_counts(total, _normalize_weights(weights))
    pool: list[str] = []
    for name, count in counts.items():
        pool.extend([name] * count)
    return pool


def _generate_equity_curve(
    payload: MonthlyReportPayload,
    trade_pnls: list[float],
) -> tuple[list[float], list[str]]:
    rng = random.Random(202605)
    periods = _infer_period_length(payload.period)
    day_pnls = [0.0 for _ in range(periods)]
    weights = [1.0 + (index / max(periods - 1, 1)) * 0.35 for index in range(periods)]
    for pnl in trade_pnls:
        day_index = rng.choices(range(periods), weights=weights, k=1)[0]
        day_pnls[day_index] += pnl
    curve: list[float] = []
    running = payload.capital_initial
    for pnl in day_pnls:
        running += pnl
        curve.append(round(running, 2))
    if curve:
        curve[-1] = round(payload.capital_final, 2)
    return curve, _build_timeline_labels(periods)


def _build_timeline_labels(periods: int) -> list[str]:
    if periods <= 8:
        return [f"J{index + 1}" for index in range(periods)]
    labels = []
    step = max(1, round(periods / 6))
    for index in range(periods):
        if index == 0 or index == periods - 1 or index % step == 0:
            labels.append(f"J{index + 1}")
        else:
            labels.append("")
    return labels


def _infer_period_length(period: str) -> int:
    match = re.search(r"([A-Za-zéûôîäöüàèùç]+)\s+(\d{4})", period, re.IGNORECASE)
    if not match:
        return 30
    month_name = match.group(1).lower()
    month = MONTH_LOOKUP.get(month_name)
    if month is None:
        return 30
    return calendar.monthrange(int(match.group(2)), month)[1]


def _generate_drawdown_curve(payload: MonthlyReportPayload) -> list[float]:
    drawdowns = []
    peak = payload.capital_initial
    for equity in payload.equity_curve:
        peak = max(peak, equity)
        drawdown = 0.0 if peak == 0 else ((equity - peak) / peak) * 100
        drawdowns.append(round(drawdown, 2))
    if not drawdowns:
        return [0.0]
    observed = min(drawdowns)
    target = payload.max_drawdown_pct
    if observed >= 0:
        drawdowns[-1] = target
        observed = target
    if observed < 0 and abs(observed) > 0:
        factor = abs(target) / abs(observed)
        drawdowns = [round(min(0.0, value * factor), 2) for value in drawdowns]
        drawdowns[drawdowns.index(min(drawdowns))] = round(target, 2)
    return drawdowns


def _aggregate_regimes(trades: list[TradeRow]) -> list[RegimeRow]:
    grouped: defaultdict[str, list[TradeRow]] = defaultdict(list)
    for trade in trades:
        grouped[trade.regime].append(trade)
    total = len(trades)
    rows: list[RegimeRow] = []
    for regime, items in grouped.items():
        avg_pnl = fmean(trade.pnl for trade in items)
        win_rate = (sum(1 for trade in items if trade.pnl > 0) / len(items)) * 100
        rows.append(
            RegimeRow(
                name=regime,
                share_pct=round((len(items) / total) * 100, 1),
                avg_pnl=round(avg_pnl, 2),
                win_rate=round(win_rate, 1),
                note=_regime_note(regime, avg_pnl),
            )
        )
    return sorted(rows, key=lambda row: row.share_pct, reverse=True)


def _regime_note(regime: str, avg_pnl: float) -> str:
    if avg_pnl >= 3:
        return f"{regime} favorable, capture d'inefficiences rapide."
    if avg_pnl > 0:
        return f"{regime} exploitable mais plus sélectif."
    return f"{regime} fragile, réduire l'agressivité et le sizing."


def _bucketize(
    values: list[float],
    bucket_count: int,
    label_formatter: str = "range",
) -> tuple[list[str], list[int]]:
    if not values:
        return ["N/A"], [0]
    low = min(values)
    high = max(values)
    if math.isclose(low, high):
        return [f"{low:.0f}"], [len(values)]
    step = (high - low) / bucket_count
    edges = [low + step * index for index in range(bucket_count + 1)]
    counts = [0 for _ in range(bucket_count)]
    for value in values:
        bucket = min(int((value - low) / step), bucket_count - 1)
        counts[bucket] += 1
    labels = []
    for index in range(bucket_count):
        start = edges[index]
        end = edges[index + 1]
        if label_formatter == "size":
            labels.append(f"{start:.0f}-{end:.0f}")
        else:
            labels.append(f"{start:+.0f}/{end:+.0f}")
    return labels, counts


def _axis_formatter(kind: str, compact: bool = False):
    def formatter(value: float) -> str:
        if kind == "currency":
            return f"{value:,.0f}" if compact else f"${value:,.0f}"
        if kind == "percent":
            return f"{value:.0f}%"
        return f"{value:,.0f}"

    return formatter


def _truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 1)].rstrip() + "…"
