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

    def dsn(self) -> str:
        return self.database_url.replace("postgresql+psycopg://", "postgresql://")


@lru_cache
def get_settings() -> Settings:
    return Settings()
