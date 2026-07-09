"""redis.asyncio client, created lazily so the app (and tests) can run without
Redis — the app.db.pool pattern. The instance is loopback-bound (NFR-22); the
on-demand job store/queue it backs (task #20, ADR-009) carries ticker+run_id
only, never secrets."""

import redis.asyncio as redis

from app.core.config import get_settings

_client: redis.Redis | None = None


async def get_redis() -> redis.Redis:
    global _client
    if _client is None:
        _client = redis.from_url(get_settings().redis_url, decode_responses=True)
    return _client


async def close_redis() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
