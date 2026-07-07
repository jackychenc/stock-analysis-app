"""Fail-closed boot check (A8 P1-SEC-1): weak/default JWT_SECRET must not boot."""

import pytest

from app.core.config import get_settings
from app.main import create_app


def _boot_with_secret(monkeypatch, secret: str):
    monkeypatch.setenv("JWT_SECRET", secret)
    get_settings.cache_clear()
    return create_app()


def test_short_jwt_secret_refuses_boot(monkeypatch):
    with pytest.raises(RuntimeError, match="JWT_SECRET"):
        _boot_with_secret(monkeypatch, "too-short")


def test_dev_default_jwt_secret_refuses_boot(monkeypatch):
    with pytest.raises(RuntimeError, match="JWT_SECRET"):
        _boot_with_secret(monkeypatch, "dev-only-secret-do-not-use-in-prod-padded-to-32b")


def test_strong_jwt_secret_boots(monkeypatch):
    app = _boot_with_secret(monkeypatch, "0123456789abcdef" * 4)
    assert app.title
