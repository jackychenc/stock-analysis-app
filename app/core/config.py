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
