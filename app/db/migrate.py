"""Schema bootstrap + plain-SQL migration runner (boring on purpose).

In Docker, db/schema.sql is loaded by the Postgres image on first init
(docker-entrypoint-initdb.d). This runner covers the non-Docker local path
and future incremental migrations:
  1. applies db/schema.sql once (tracked as 'schema.sql@v1.0'),
  2. then applies db/migrations/*.sql in filename order.

Usage: python -m app.db.migrate
"""

import asyncio
import pathlib
import sys

import asyncpg

from app.core.config import get_settings

DB_DIR = pathlib.Path(__file__).resolve().parents[2] / "db"
SCHEMA_KEY = "schema.sql@v1.0"


async def run() -> list[str]:
    conn = await asyncpg.connect(get_settings().dsn())
    applied: list[str] = []
    try:
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " filename TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
        done = {r["filename"] for r in await conn.fetch("SELECT filename FROM schema_migrations")}

        schema_present = await conn.fetchval("SELECT to_regclass('public.ticker') IS NOT NULL")
        if SCHEMA_KEY not in done:
            if not schema_present:
                await conn.execute((DB_DIR / "schema.sql").read_text())
                applied.append(SCHEMA_KEY)
            await conn.execute(
                "INSERT INTO schema_migrations (filename) VALUES ($1)"
                " ON CONFLICT DO NOTHING", SCHEMA_KEY,
            )

        migrations_dir = DB_DIR / "migrations"
        if migrations_dir.is_dir():
            for path in sorted(migrations_dir.glob("*.sql")):
                if path.name in done:
                    continue
                async with conn.transaction():
                    await conn.execute(path.read_text())
                    await conn.execute(
                        "INSERT INTO schema_migrations (filename) VALUES ($1)", path.name
                    )
                applied.append(path.name)
    finally:
        await conn.close()
    return applied


if __name__ == "__main__":
    names = asyncio.run(run())
    print(f"applied: {names}" if names else "up to date")
    sys.exit(0)
