from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from app.core.config import Settings, get_settings
from app.core.security import (
    issue_jwt_pair,
    issue_session_token,
    read_jwt,
    verify_credentials,
)
from app.schemas.contracts import LoginRequest, TokenBundle

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=TokenBundle)
def login(
    body: LoginRequest, response: Response, settings: Settings = Depends(get_settings)
) -> TokenBundle:
    """One verifier, two strategies: web gets a session cookie (empty bundle);
    iOS gets JWT access + refresh tokens. [ADR-002]"""
    if not verify_credentials(body.username, body.password, settings):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "UNAUTHORIZED", "message": "Invalid username or password."},
        )
    if body.client == "web":
        response.set_cookie(
            key=settings.session_cookie_name,
            value=issue_session_token(body.username, settings),
            max_age=settings.session_ttl_seconds,
            httponly=True,
            samesite="lax",
        )
        return TokenBundle()
    pair = issue_jwt_pair(body.username, settings)
    return TokenBundle(**pair)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(response: Response, settings: Settings = Depends(get_settings)) -> None:
    response.delete_cookie(settings.session_cookie_name)


@router.post("/refresh", response_model=TokenBundle)
def refresh(request: Request, settings: Settings = Depends(get_settings)) -> TokenBundle:
    """iOS: exchange a valid refresh token (Bearer) for a new JWT pair."""
    auth = request.headers.get("Authorization", "")
    token = auth.removeprefix("Bearer ") if auth.startswith("Bearer ") else ""
    user = read_jwt(token, settings, expected_type="refresh")
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "UNAUTHORIZED", "message": "Refresh token invalid or expired."},
        )
    return TokenBundle(**issue_jwt_pair(user, settings))
