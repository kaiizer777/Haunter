"""Verify the users table was created correctly in Neon."""
import asyncio
from sqlalchemy import text
from app.db import engine


async def check() -> None:
    async with engine.begin() as conn:
        r = await conn.execute(
            text(
                "SELECT column_name, data_type "
                "FROM information_schema.columns "
                "WHERE table_name='users' "
                "ORDER BY ordinal_position"
            )
        )
        rows = r.fetchall()
        if not rows:
            print("ERROR: users table not found")
            return
        print("users table columns:")
        for col_name, data_type in rows:
            print(f"  {col_name:25} {data_type}")


asyncio.run(check())
