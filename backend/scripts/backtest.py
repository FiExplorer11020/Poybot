import asyncio
import random
import time
from app.services.adaptive_strategy import AdaptiveStrategyEngine, PortfolioState, RiskConfig

def generate_synthetic_market_data(ticks=2000):
    """Generates realistic Polymarket order book data using a mean-reverting random walk."""
    mid_price = 0.50
    data = []
    
    # Simulate a market that slowly trends up, with occasional high-volatility shocks
    for i in range(ticks):
        # 1% chance of a volatility shock
        if random.random() < 0.01:
            step = random.gauss(0, 0.05)
            spread = random.uniform(0.08, 0.15) # Spreads widen during shocks
        else:
            step = random.gauss(0.0001, 0.005) # Slight upward drift
            spread = random.uniform(0.02, 0.07) # Normal spreads
            
        mid_price = max(0.01, min(0.99, mid_price + step))
        best_bid = max(0.01, mid_price - (spread / 2))
        best_ask = min(0.99, mid_price + (spread / 2))
        
        data.append({"tick": i, "best_bid": best_bid, "best_ask": best_ask, "spread": spread, "mid": mid_price})
    return data

async def run_backtest():
    print("=========================================")
    print("Starting Deep Analytics Backtest (2000 iterations)")
    
    # Initialize Engine with current config
    engine = AdaptiveStrategyEngine(RiskConfig())
    portfolio = PortfolioState(equity=25000.00, capital_in_trade=0.0, total_pnl=0.0)
    
    market_data = generate_synthetic_market_data(2000)
    
    trades = []
    active_trade = None
    
    print("Pumping synthetic order book through AdaptiveStrategyEngine...")
    
    for i, tick in enumerate(market_data):
        eval_out = engine.evaluate_market("sim_market_1", tick["best_bid"], tick["best_ask"])
        
        # Continuous Execution & Closing Logic
        if active_trade:
            # Check closing conditions: edge vanishes or time elapsed > 45 ticks
            holding_time = i - active_trade["entry_tick"]
            if eval_out["expected_edge"] <= 0.001 or holding_time > 45:
                # Close trade
                exit_price = tick["best_bid"] if active_trade["side"] == "BUY_YES" else (1 - tick["best_ask"])
                # PnL roughly = (exit - entry) * size - fees
                raw_pnl = (exit_price - active_trade["entry_price"]) * active_trade["size"]
                if active_trade["side"] == "BUY_NO":
                    raw_pnl = ((1-tick["best_ask"]) - (1-active_trade["entry_price"])) * active_trade["size"]
                    
                net_pnl = raw_pnl - active_trade["fees"]
                
                portfolio.total_pnl += net_pnl
                portfolio.equity += net_pnl
                portfolio.capital_in_trade = 0.0
                
                trades.append({
                    "entry_tick": active_trade["entry_tick"],
                    "exit_tick": i,
                    "holding_time": holding_time,
                    "side": active_trade["side"],
                    "entry_price": active_trade["entry_price"],
                    "exit_price": exit_price,
                    "pnl": net_pnl
                })
                active_trade = None
        
        elif eval_out["detected"]:
            # Entering new trade
            try:
                notional, risk_pct = engine.size_position(portfolio, eval_out["expected_edge"])
                if notional > 0:
                    side = eval_out["direction"]
                    entry_price = tick["best_ask"] if side == "BUY_YES" else (1 - tick["best_bid"])
                    if entry_price > 0:
                        size = notional / entry_price
                        fees = notional * (engine.cfg.fee_bps / 10000)
                        
                        portfolio.capital_in_trade = notional
                        active_trade = {
                            "entry_tick": i,
                            "side": side,
                            "entry_price": entry_price,
                            "size": size,
                            "notional": notional,
                            "fees": fees
                        }
            except ValueError:
                pass
                
    # Close orphaned trade at end
    if active_trade:
        tick = market_data[-1]
        exit_price = tick["best_bid"] if active_trade["side"] == "BUY_YES" else (1 - tick["best_ask"])
        raw_pnl = (exit_price - active_trade["entry_price"]) * active_trade["size"]
        if active_trade["side"] == "BUY_NO":
            raw_pnl = ((1-tick["best_ask"]) - (1-active_trade["entry_price"])) * active_trade["size"]
        net_pnl = raw_pnl - active_trade["fees"]
        trades.append({"entry_tick": active_trade["entry_tick"], "exit_tick": 2000, "holding_time": 2000 - active_trade["entry_tick"], "side": active_trade["side"], "entry_price": active_trade["entry_price"], "exit_price": exit_price, "pnl": net_pnl})

    print("=========================================")
    print("Backtest Completed Succesfully!")
    print(f"Total Ticks: 2000")
    print(f"Trades Executed: {len(trades)}")
    
    if trades:
        wins = sum(1 for t in trades if t["pnl"] > 0)
        win_rate = (wins / len(trades)) * 100
        avg_holding = sum(t["holding_time"] for t in trades) / len(trades)
        gross_profit = sum(t["pnl"] for t in trades if t["pnl"] > 0)
        gross_loss = sum(t["pnl"] for t in trades if t["pnl"] < 0)
        profit_factor = abs(gross_profit / gross_loss) if gross_loss != 0 else float('inf')
        
        print(f"Winning Trades: {wins}")
        print(f"Win Rate: {win_rate:.1f}%")
        print(f"Average Holding Time: {avg_holding:.1f} ticks")
        print(f"Profit Factor: {profit_factor:.2f}")
        print(f"Net PnL: ${portfolio.total_pnl:,.2f}")
        
        print("\n=== PATTERN ANALYSIS & OPTIMIZATIONS ===")
        if win_rate < 50:
            print("OBSERVATION: Win rate is sub-50%. Spread costs (slippage) are eating into the momentum.")
            print("RECOMMENDATION: Increase 'base_entry_threshold' to >0.01 to demand higher expected value before entering.")
        elif avg_holding > 30:
            print("OBSERVATION: Average holding time is high. Edges are reverting against the position.")
            print("RECOMMENDATION: We should implement an aggressive Trailing Take-Profit instead of waiting 45 ticks.")
        else:
            print("OBSERVATION: Strategy is capturing quick micro-momentum effectively.")
            print("RECOMMENDATION: Increase Kelly Fraction to scale sizing into these high-confidence setups.")
    else:
        print("Win Rate: 0.0% (No trades taken)")
        print("OBSERVATION: The edge threshold constraints are still too tight for the simulated liquidity.")
        print("RECOMMENDATION: Lower 'spread_cap' requirements or increase the volatility multiplier.")

if __name__ == "__main__":
    asyncio.run(run_backtest())
