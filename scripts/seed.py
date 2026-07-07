"""Seed tickers for local testing (Foundation acceptance: seed ticker present).

Usage: python scripts/seed.py
"""

import asyncio
import sys

sys.path.insert(0, ".")
import asyncpg  # noqa: E402

from app.core.config import get_settings  # noqa: E402

SEED_TICKERS = [
    # (symbol, exchange, full_symbol, name, sector)
    ("2330", "TWSE", "2330.TW", "Taiwan Semiconductor Manufacturing Co.", "Semiconductors"),
    ("6488", "TPEx", "6488.TWO", "GlobalWafers Co.", "Semiconductors"),
    ("AAPL", "US", "AAPL", "Apple Inc.", "Technology"),
]


async def main() -> None:
    conn = await asyncpg.connect(get_settings().dsn())
    try:
        for symbol, exchange, full_symbol, name, sector in SEED_TICKERS:
            await conn.execute(
                """
                INSERT INTO ticker (symbol, exchange, full_symbol, name, sector)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (full_symbol) DO NOTHING
                """,
                symbol, exchange, full_symbol, name, sector,
            )
        count = await conn.fetchval("SELECT count(*) FROM ticker")
        print(f"seeded; ticker rows: {count}")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
