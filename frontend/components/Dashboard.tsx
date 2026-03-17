"use client";

import { useEffect, useMemo, useState } from "react";
import { Area, AreaChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { LiveSnapshot } from "../lib/types";

const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8000";

const emptyState: LiveSnapshot = {
  bot: { status: "STOPPED", uptime_seconds: 0, latency_ms: 0 },
  stats: { total_pnl: 0, win_rate: 0, avg_profit: 0, active_markets: 0, detected_arbs_today: 0 },
  markets: [],
  price_history: [],
  recent_simulations: [],
};

export default function Dashboard() {
  const [state, setState] = useState<LiveSnapshot>(emptyState);

  useEffect(() => {
    fetch(`${API_BASE}/api/v1/live-summary`).then((r) => r.json()).then((r) => setState(r.data));
    const ws = new WebSocket(`${API_BASE.replace("http", "ws")}/ws/live`);
    ws.onmessage = (event) => {
      const msg = JSON.parse(event.data);
      if (msg.type === "bootstrap" || msg.type === "control") setState(msg.payload);
      if (msg.type === "tick") {
        setState((prev) => ({
          ...prev,
          bot: { ...prev.bot, latency_ms: msg.payload.latency_ms },
          stats: msg.payload.stats,
          markets: msg.payload.markets,
          price_history: [...prev.price_history.slice(-119), msg.payload.price_point],
        }));
      }
      if (msg.type === "simulation") {
        setState((prev) => ({ ...prev, recent_simulations: [msg.payload, ...prev.recent_simulations].slice(0, 10) }));
      }
    };
    const keepAlive = setInterval(() => ws.readyState === 1 && ws.send("ping"), 10000);
    return () => {
      clearInterval(keepAlive);
      ws.close();
    };
  }, []);

  const uptime = useMemo(() => `${Math.floor(state.bot.uptime_seconds / 60)}m ${state.bot.uptime_seconds % 60}s`, [state.bot.uptime_seconds]);

  const control = async (command: string) => {
    await fetch(`${API_BASE}/api/v1/bot/control`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ command }),
    });
  };

  const simulate = async (marketId: string) => {
    await fetch(`${API_BASE}/api/v1/markets/${marketId}/simulate-exec`, { method: "POST" });
  };

  return (
    <div className="container">
      <div className="header">
        <strong>POLYMARKET ARB BOT MVP</strong>
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <span className={`badge ${state.bot.status === "RUNNING" ? "run" : state.bot.status === "PAUSED" ? "pause" : "stop"}`}>{state.bot.status}</span>
          <span>Uptime {uptime}</span>
          <span>Latency {state.bot.latency_ms}ms</span>
          <button className="btn secondary" onClick={() => control("start")}>START</button>
          <button className="btn secondary" onClick={() => control("pause")}>PAUSE</button>
          <button className="btn secondary" onClick={() => control("stop")}>STOP</button>
        </div>
      </div>

      <div className="grid">
        <div className="stats">
          {[
            ["Total P&L", state.stats.total_pnl],
            ["Win Rate", `${state.stats.win_rate}%`],
            ["Avg Profit", state.stats.avg_profit],
            ["Active Markets", state.stats.active_markets],
            ["Detected Arbs", state.stats.detected_arbs_today],
          ].map(([label, value]) => (
            <div className="card" key={String(label)}>
              <div>{label}</div><div className="stat-value">{String(value)}</div>
            </div>
          ))}
        </div>

        <div>
          <div className="chart-wrap" style={{ height: 250 }}>
            <ResponsiveContainer width="100%" height="100%">
              <AreaChart data={state.price_history}>
                <XAxis dataKey="timestamp" hide />
                <YAxis domain={[0, 1]} />
                <Tooltip />
                <Area dataKey="value" stroke="#10b981" fill="#064e3b" />
              </AreaChart>
            </ResponsiveContainer>
          </div>

          <div className="market-grid">
            {state.markets.map((market) => (
              <div className="card" key={market.market_id}>
                <div className="market-title">{market.title}</div>
                <span className={`badge ${market.detected ? "run" : "pause"}`}>{market.detected ? "DETECTED" : "WATCH"}</span>
                <div className="row"><span>Bid</span><span>{market.best_bid.toFixed(3)}</span></div>
                <div className="row"><span>Ask</span><span>{market.best_ask.toFixed(3)}</span></div>
                <div className="row"><span>Spread</span><span>{market.spread.toFixed(3)}</span></div>
                <div className="row"><span>Est. Profit</span><span>{market.est_profit.toFixed(2)}</span></div>
                <button className="btn" onClick={() => simulate(market.market_id)}>Simulate Exec</button>
              </div>
            ))}
          </div>
        </div>
      </div>

      <div className="table-wrap">
        <h4>Latest simulations</h4>
        <table>
          <thead><tr><th>ID</th><th>Market</th><th>Side</th><th>Price</th><th>Size</th><th>P&L</th><th>Timestamp</th></tr></thead>
          <tbody>
            {state.recent_simulations.map((row) => (
              <tr key={row.id}><td>{row.id}</td><td>{row.market_id}</td><td>{row.side}</td><td>{row.price}</td><td>{row.size}</td><td>{row.pnl}</td><td>{row.timestamp}</td></tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
