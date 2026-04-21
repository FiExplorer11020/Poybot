"""
Bootstrap the leader registry from Polymarket's public data API.

Used when the Falcon API is unavailable. Fetches recent high-volume wallets
from data-api.polymarket.com/trades and seeds them into the leaders table.

Usage: python scripts/bootstrap_leaders.py [--pages N]
"""

import asyncio
import json
import os
from collections import defaultdict
from datetime import datetime, timezone

import aiohttp
import asyncpg

DATA_API = "https://data-api.polymarket.com"
DB_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://polymarket:polymarket_dev_password@localhost:5432/polymarket",
)


async def fetch_top_wallets(pages: int = 10) -> list[dict]:
    """Fetch recent trades and rank wallets by volume + trade count."""
    wallets: dict[str, dict] = defaultdict(lambda: {"trades": 0, "volume": 0.0, "markets": set()})

    async with aiohttp.ClientSession() as session:
        for page in range(pages):
            offset = page * 500
            url = f"{DATA_API}/trades?limit=500&offset={offset}"
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as r:
                    if r.status != 200:
                        break
                    trades = json.loads(await r.text())
                    if not trades:
                        break
                    for t in trades:
                        w = t.get("proxyWallet", "")
                        if not w:
                            continue
                        size = float(t.get("size", 0)) * float(t.get("price", 0))
                        cid = t.get("conditionId", "")
                        wallets[w]["trades"] += 1
                        wallets[w]["volume"] += size
                        if cid:
                            wallets[w]["markets"].add(cid)
            except Exception as e:
                print(f"  Warning: page {page} failed: {e}")
                break

    # Score = volume * log(trades) to balance size + activity
    import math

    ranked = []
    for wallet, stats in wallets.items():
        score = stats["volume"] * math.log(max(stats["trades"], 1) + 1)
        ranked.append(
            {
                "wallet_address": wallet,
                "falcon_score": round(min(score / 100, 100), 4),  # Normalize to 0-100
                "trades": stats["trades"],
                "volume": round(stats["volume"], 2),
                "markets": len(stats["markets"]),
            }
        )
    ranked.sort(key=lambda x: -x["falcon_score"])
    return ranked


async def seed_leaders(wallets: list[dict], top_n: int = 200) -> int:
    """Insert top_n wallets into the leaders table."""
    conn = await asyncpg.connect(DB_URL)
    inserted = 0
    try:
        for w in wallets[:top_n]:
            classification = {
                "strategy": "unknown",
                "influence": "top_trader" if w["falcon_score"] > 5 else "community",
                "horizon": "unknown",
                "copiable": True,
                "classified_at": datetime.now(tz=timezone.utc).isoformat(),
                "source": "bootstrap",
            }
            await conn.execute(
                """
                INSERT INTO leaders
                    (wallet_address, falcon_score, classification_json, on_watchlist, excluded)
                VALUES ($1, $2, $3::jsonb, TRUE, FALSE)
                ON CONFLICT (wallet_address) DO UPDATE SET
                    falcon_score        = EXCLUDED.falcon_score,
                    classification_json = EXCLUDED.classification_json,
                    last_refresh        = NOW()
                """,
                w["wallet_address"],
                w["falcon_score"],
                json.dumps(classification),
            )
            inserted += 1
    finally:
        await conn.close()
    return inserted


async def main(pages: int = 10):
    print(f"Fetching top wallets from {pages} pages of recent Polymarket trades...")
    wallets = await fetch_top_wallets(pages=pages)
    print(f"  Found {len(wallets)} unique wallets")

    top20 = wallets[:20]
    print("\nTop 20 wallets by activity score:")
    for i, w in enumerate(top20, 1):
        print(
            f"  {i:2d}. {w['wallet_address']}  "
            f"score={w['falcon_score']:.2f}  "
            f"trades={w['trades']}  "
            f"vol=${w['volume']:,.0f}  "
            f"markets={w['markets']}"
        )

    print("\nSeeding top 200 wallets into leaders table...")
    n = await seed_leaders(wallets, top_n=200)
    print(f"  Inserted/updated {n} leaders")
    print("Bootstrap complete.")


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("--pages", type=int, default=10, help="Pages of 500 trades to fetch")
    args = p.parse_args()
    asyncio.run(main(pages=args.pages))
