"""
Applies all SQL migrations in docs/migrations/ in order.
Tracks applied migrations in a schema_migrations table — idempotent.

Usage: python scripts/setup_db.py
"""

import asyncio
import os
from pathlib import Path

import asyncpg

MIGRATIONS_DIR = Path(__file__).parent.parent / "docs" / "migrations"


async def main():
    db_url = os.getenv(
        "DATABASE_URL",
        "postgresql://polymarket:polymarket_dev_password@localhost:5432/polymarket",
    )
    conn = await asyncpg.connect(db_url)
    try:
        # Create migrations tracking table if it doesn't exist
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version     INTEGER PRIMARY KEY,
                applied_at  TIMESTAMPTZ DEFAULT NOW()
            )
        """)

        migration_files = sorted(MIGRATIONS_DIR.glob("*.sql"))
        for migration_file in migration_files:
            # Extract version number from filename (e.g., 001_schema.sql → 1)
            version_str = migration_file.name.split("_")[0]
            try:
                version = int(version_str)
            except ValueError:
                print(f"  ⚠ Skipping {migration_file.name} (no version prefix)")
                continue

            already_applied = await conn.fetchval(
                "SELECT 1 FROM schema_migrations WHERE version = $1",
                version,
            )
            if already_applied:
                print(f"  ↷ {migration_file.name} (v{version}) already applied, skipping")
                continue

            print(f"Applying {migration_file.name} (v{version})...")
            sql = migration_file.read_text()
            await conn.execute(sql)
            await conn.execute(
                "INSERT INTO schema_migrations (version) VALUES ($1)",
                version,
            )
            print(f"  ✓ {migration_file.name} applied")

        print("\nAll migrations applied successfully.")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
