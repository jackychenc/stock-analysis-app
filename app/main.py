from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api.routers import auth, config, health, pipeline, recommendations, stocks
from app.core.config import get_settings
from app.db.pool import close_pool

API_PREFIX = "/api/v1"  # openapi.yaml servers: /api/v1


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await close_pool()


def create_app() -> FastAPI:
    settings = get_settings()
    # Fail closed (A8 P1-SEC-1): a weak or default signing secret must not boot.
    if len(settings.jwt_secret) < 32 or settings.jwt_secret.startswith("dev-only"):
        raise RuntimeError(
            "JWT_SECRET must be >=32 bytes and not a dev default. "
            'Generate one: python3 -c "import secrets; print(secrets.token_hex(32))"'
        )
    app = FastAPI(
        title="Stock Investment Analysis App API",
        version="1.0.0",
        description="Personal decision-support, not financial advice (FR-39).",
        lifespan=lifespan,
    )

    # FR-39 criterion 6: header == payload, byte-identical. Canonical text is
    # ASCII-only by A8 ruling; fail fast if a config override breaks that.
    try:
        settings.disclaimer_text.encode("ascii")
    except UnicodeEncodeError as exc:
        raise RuntimeError(
            "DISCLAIMER_TEXT must be ASCII-only (FR-39 criterion 6: payload =="
            " X-Disclaimer header, and HTTP headers are latin-1)."
        ) from exc

    @app.middleware("http")
    async def disclaimer_header(request: Request, call_next):
        """FR-39: the disclaimer accompanies every API response."""
        response = await call_next(request)
        response.headers["X-Disclaimer"] = settings.disclaimer_text
        response.headers["X-Disclaimer-Version"] = settings.disclaimer_version
        return response

    @app.exception_handler(Exception)
    async def total_outage_handler(request: Request, exc: Exception) -> JSONResponse:
        """503 is reserved for total outage — e.g. the database is down.
        A single failed scoring module is NEVER a 503 (contract §global)."""
        return JSONResponse(
            status_code=503,
            content={"code": "TOTAL_OUTAGE", "message": "Service temporarily unavailable."},
        )

    app.include_router(health.router)  # unprefixed: /healthz
    for router in (auth.router, stocks.router, recommendations.router,
                   config.router, pipeline.router):
        app.include_router(router, prefix=API_PREFIX)

    app.state.settings = settings
    return app


app = create_app()
