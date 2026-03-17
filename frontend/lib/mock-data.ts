type RiskLevel = "normal" | "high";

type ScannerCard = {
  market: string;
  category: string;
  bid: number;
  ask: number;
  spread: number;
  implied: number;
  profit: number;
  exposure: number;
  detected: boolean;
  risk: RiskLevel;
};

export const scannerCards: ScannerCard[] = [
  { market: "BTC above 100k by Dec 31", category: "Crypto", bid: 0.52, ask: 0.55, spread: 0.03, implied: 0.54, profit: 3.2, exposure: 9.5, detected: true, risk: "normal" },
  { market: "US CPI below 2.8% next print", category: "Politics", bid: 0.48, ask: 0.51, spread: 0.03, implied: 0.50, profit: 2.4, exposure: 5.8, detected: true, risk: "normal" },
  { market: "ETH outperforms BTC this month", category: "Crypto", bid: 0.41, ask: 0.47, spread: 0.06, implied: 0.44, profit: 1.1, exposure: 3.5, detected: false, risk: "high" },
  { market: "Fed cut before Q4", category: "Politics", bid: 0.57, ask: 0.60, spread: 0.03, implied: 0.59, profit: 2.9, exposure: 7.2, detected: true, risk: "normal" },
  { market: "SOL above 250 this quarter", category: "Crypto", bid: 0.39, ask: 0.45, spread: 0.06, implied: 0.42, profit: 0.8, exposure: 2.0, detected: false, risk: "high" },
  { market: "US recession in 2026", category: "Politics", bid: 0.29, ask: 0.34, spread: 0.05, implied: 0.32, profit: 1.6, exposure: 4.7, detected: true, risk: "high" },
  { market: "NASDAQ +8% this quarter", category: "Crypto", bid: 0.61, ask: 0.63, spread: 0.02, implied: 0.62, profit: 3.7, exposure: 10.1, detected: true, risk: "normal" },
  { market: "Oil below 65 by next month", category: "High-Risk", bid: 0.23, ask: 0.31, spread: 0.08, implied: 0.27, profit: 1.0, exposure: 1.8, detected: false, risk: "high" }
];

export const pnlSeries = Array.from({ length: 50 }).map((_, i) => ({
  t: `T${i + 1}`,
  pnl: 2400 + i * 18 + Math.sin(i / 4) * 220,
  drawdown: -Math.abs(Math.sin(i / 7) * 8)
}));

export const detailedTrades = Array.from({ length: 20 }).map((_, i) => ({
  timestamp: `2026-03-17 12:${String(i).padStart(2, "0")}:15.${String(100 + i).padStart(3, "0")}`,
  marketTitle: scannerCards[i % scannerCards.length].market,
  conditionId: `cond_${10000 + i}`,
  tokenId: `tok_${32000 + i}`,
  side: i % 2 ? "BUY" : "SELL",
  sizeShares: (120 + i * 3).toFixed(2),
  sizeUsd: (220 + i * 14).toFixed(2),
  entryPrice: (0.35 + (i % 10) * 0.03).toFixed(3),
  impliedEntry: (0.4 + (i % 9) * 0.04).toFixed(3),
  trigger: i % 3 ? "SpreadCollapse" : "CrossMarketDrift",
  kelly: (0.2 + (i % 4) * 0.1).toFixed(2),
  riskPct: (0.6 + (i % 5) * 0.35).toFixed(2),
  estProfitRaw: (2.1 + (i % 6) * 0.8).toFixed(2),
  estProfitAdj: (1.3 + (i % 6) * 0.55).toFixed(2),
  slippage: (0.02 + (i % 4) * 0.01).toFixed(3),
  fees: (0.3 + (i % 4) * 0.06).toFixed(2),
  txHash: `0xabc${(100000 + i).toString(16)}def`,
  status: i % 4 ? "FILLED" : "CANCELLED",
  latency: `${42 + (i % 6) * 9}ms`,
  postDelta: (i % 2 ? 1 : -1) * (0.2 + (i % 5) * 0.3)
}));
