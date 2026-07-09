from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "Stock Investment Analysis App"
    env: str = "local"
    methodology_version: str = "mvp-1.0"

    # Compose passes SQLAlchemy-style postgresql+psycopg://; we use asyncpg,
    # so the driver suffix is stripped in dsn().
    database_url: str = "postgresql://stockapp:stockapp@localhost:5432/stockapp"
    redis_url: str = "redis://localhost:6379/0"

    # Auth — single-user app. One credential verifier, two token strategies
    # (session cookie for web, JWT access+refresh for iOS). [ADR-002]
    jwt_secret: str = "dev-only-secret-do-not-use-in-prod"
    admin_username: str = "admin"
    # PBKDF2 hash from scripts/hash_password.py; empty disables login.
    admin_password_hash: str = ""

    access_token_ttl_seconds: int = 15 * 60
    refresh_token_ttl_seconds: int = 14 * 24 * 3600
    session_cookie_name: str = "session"  # matches openapi.yaml cookieAuth
    session_ttl_seconds: int = 12 * 3600

    # pgcrypto key for personal financial data (NFR-05). Env-sourced locally;
    # Key Vault in a future cloud phase (KeyProvider abstraction, ADR-004).
    app_encryption_key: str = ""

    # FR-19 / A6 bucket 4: deterministic ingestion without network. CI and
    # stack smokes set YFINANCE_FIXTURE_MODE=true; production leaves it off.
    yfinance_fixture_mode: bool = False
    # Task #9 (T9-D1/D2): same fixture switch per chip source — no live
    # TWSE/TPEx or EDGAR network in CI.
    twse_tpex_fixture_mode: bool = False
    edgar_fixture_mode: bool = False
    # Task #12: same fixture switch for the GDELT news source — no live
    # GDELT DOC API network in CI (env GDELT_FIXTURE_MODE).
    gdelt_fixture_mode: bool = False
    # PM condition (task #9): curated 13F filers + CUSIP map are config data,
    # editable without a deploy; validated at load (A8).
    curated_13f_path: str = "config/curated_13f.json"
    # Task #12 (#9 curated-config precedent): curated GDELT query phrases per
    # ticker are config data, editable without a deploy; validated at load (A8).
    news_queries_path: str = "config/news_queries.json"

    # Task #20 (ADR-009 / FR-61/62): on-demand ticker analysis.
    # - max_coverage_pool_size: FR-61 cap on the COVERED universe (benchmarks
    #   are is_covered=false and never count);
    # - on_demand_cooldown_s: a just-analyzed ticker is served from snapshot —
    #   force bypasses the fresh short-circuit but honors this cooldown;
    # - analyze_poll_after_ms: the poll hint POST /analyze hands clients.
    max_coverage_pool_size: int = 25
    on_demand_cooldown_s: int = 600
    analyze_poll_after_ms: int = 2000
    # In-process queue worker (FastAPI lifespan task). Tests disable it so the
    # background consumer never races route-level job-state assertions.
    analysis_worker_enabled: bool = True

    # FR-39: compliance-owned config (A8) — wording changes are a config
    # change, not a contract change. Canonical text is ASCII-ONLY by A8's
    # final ruling (2026-07-08) so payload == X-Disclaimer header holds
    # byte-identical. Env-overridable via DISCLAIMER_TEXT.
    disclaimer_text: str = (
        "For personal decision-support and educational use only. Not personalized "
        "investment advice, and not a solicitation or recommendation to buy or sell "
        "any security. Not provided by a registered investment adviser (US Investment "
        "Advisers Act) or a Securities Investment Consulting Enterprise (Taiwan). "
        "Signals, scores and target prices are model outputs; past performance and "
        "backtests are hypothetical and do not guarantee future results. You are "
        "solely responsible for your own investment decisions; consult a licensed "
        "adviser."
    )
    # Audit trace (compliance evidence): which disclosure wording shipped.
    disclaimer_version: str = "fr39-v1"

    def dsn(self) -> str:
        return self.database_url.replace("postgresql+psycopg://", "postgresql://")


@lru_cache
def get_settings() -> Settings:
    return Settings()
