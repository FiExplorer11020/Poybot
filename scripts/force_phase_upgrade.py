"""Force error_model phase upgrade for eligible leaders.

The phase upgrade only fires on `on_position_closed` events. Historical
leaders that already crossed the threshold (positions_resolved >= 100)
never get auto-promoted. This script promotes them in batch.
"""
import asyncio
import os
import sys
import json
import asyncpg


DB_URL = os.environ["DATABASE_URL"]


async def main():
    # Import inside the script so it picks up the engine's runtime modules
    sys.path.insert(0, "/app")
    from src.profiler.error_model import ErrorModel
    from src.database.connection import initialize_pool, close_pool

    await initialize_pool(dsn=DB_URL, min_size=1, max_size=4)

    pool = await asyncpg.create_pool(DB_URL, min_size=1, max_size=2)
    eligible = []
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT wallet_address, positions_resolved
            FROM leader_profiles
            WHERE positions_resolved >= 100 AND error_model_phase = 1
            ORDER BY positions_resolved DESC
            """
        )
        for r in rows:
            eligible.append((r["wallet_address"], r["positions_resolved"]))

    print(f"Found {len(eligible)} leaders eligible for phase 2 upgrade")
    if not eligible:
        await close_pool()
        await pool.close()
        return

    em = ErrorModel()
    upgraded = 0
    failed = 0
    skipped = 0
    for wallet, n_resolved in eligible:
        try:
            phase, profile, _ = await em._load_state(wallet)
            if phase >= 2:
                skipped += 1
                continue
            await em._upgrade_phase(wallet, 2, profile)
            # Verify
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT error_model_phase FROM leader_profiles WHERE wallet_address=$1",
                    wallet,
                )
            final_phase = int(row["error_model_phase"] or 1) if row else 1
            if final_phase >= 2:
                upgraded += 1
                print(f"  ✓ {wallet[:20]}... ({n_resolved} pos) → phase {final_phase}")
            else:
                failed += 1
                print(f"  ✗ {wallet[:20]}... ({n_resolved} pos) — upgrade returned phase {final_phase}")
        except Exception as e:
            failed += 1
            print(f"  ! {wallet[:20]}... — {type(e).__name__}: {e}")

    print(f"\nDONE: upgraded={upgraded}, failed={failed}, skipped={skipped}")
    await pool.close()
    await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
