import asyncio
from app.db.base import Base
from app.db.session import engine

async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    print("SQLite database initialized successfully.")

if __name__ == "__main__":
    asyncio.run(init_db())
