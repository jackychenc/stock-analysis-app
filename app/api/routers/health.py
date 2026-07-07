from fastapi import APIRouter

from app.core.config import get_settings
from app.db.pool import db_reachable

router = APIRouter(tags=["ops"])


@router.get("/healthz")
async def healthz() -> dict:
    """Unauthenticated liveness + dependency reachability."""
    settings = get_settings()
    return {
        "status": "ok",
        "app": settings.app_name,
        "env": settings.env,
        "methodology_version": settings.methodology_version,
        "db": "ok" if await db_reachable() else "unreachable",
    }
