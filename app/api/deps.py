"""Auth dependency: accepts either the web session cookie or an iOS bearer JWT."""

from fastapi import Depends, HTTPException, Request, status

from app.core.config import Settings, get_settings
from app.core.security import read_jwt, read_session_token


def current_user(
    request: Request, settings: Settings = Depends(get_settings)
) -> str:
    cookie = request.cookies.get(settings.session_cookie_name)
    if cookie:
        user = read_session_token(cookie, settings)
        if user:
            return user

    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        user = read_jwt(auth.removeprefix("Bearer "), settings, expected_type="access")
        if user:
            return user

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"code": "UNAUTHENTICATED", "message": "Login required."},
    )
