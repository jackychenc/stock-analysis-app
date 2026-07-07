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

    # FR-39: compliance-owned config (A8) — wording changes are a config
    # change, not a contract change. Default = canonical v1 (SRS v0.2.4).
    # Env-overridable via DISCLAIMER_TEXT.
    disclaimer_text: str = (
        "For personal decision-support and educational use only. Not personalized "
        "investment advice, and not a solicitation or recommendation to buy or sell "
        "any security. Not provided by a registered investment adviser (US Investment "
        "Advisers Act) / 證券投資顧問事業 (Taiwan). Signals, scores and target prices "
        "are model outputs; past performance and backtests are hypothetical and do "
        "not guarantee future results. You are solely responsible for your own "
        "decisions — consult a licensed adviser."
    )

    def disclaimer_header_value(self) -> str:
        """HTTP headers are latin-1 (RFC 9110; Starlette enforces): the one
        non-ASCII term is rendered by its official English translation."""
        return (
            self.disclaimer_text
            .replace("證券投資顧問事業", "Securities Investment Consulting Enterprise")
            .replace("—", "-")
            .encode("latin-1", "replace")
            .decode("latin-1")
        )

    def dsn(self) -> str:
        return self.database_url.replace("postgresql+psycopg://", "postgresql://")


@lru_cache
def get_settings() -> Settings:
    return Settings()
