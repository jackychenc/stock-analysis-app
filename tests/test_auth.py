"""Auth skeleton: one verifier, two strategies (web cookie / iOS JWT+refresh)."""

from tests.conftest import TEST_PASSWORD, TEST_USER

LOGIN = "/api/v1/auth/login"


def test_web_login_sets_session_cookie(client):
    r = client.post(LOGIN, json={"username": TEST_USER, "password": TEST_PASSWORD,
                                 "client": "web"})
    assert r.status_code == 200
    assert "session" in r.cookies
    body = r.json()
    assert body["access_token"] is None  # web gets cookie, not tokens


def test_ios_login_returns_jwt_pair(client):
    r = client.post(LOGIN, json={"username": TEST_USER, "password": TEST_PASSWORD,
                                 "client": "ios"})
    assert r.status_code == 200
    body = r.json()
    assert body["access_token"] and body["refresh_token"]
    assert body["expires_in"] == 15 * 60


def test_bad_password_rejected(client):
    r = client.post(LOGIN, json={"username": TEST_USER, "password": "wrong",
                                 "client": "web"})
    assert r.status_code == 401
    assert r.json()["detail"]["code"] == "UNAUTHORIZED"


def test_protected_route_rejects_unauthenticated(client):
    r = client.get("/api/v1/stocks/2330.TW/dashboard")
    assert r.status_code == 401


def test_jwt_access_token_grants_access(client):
    tokens = client.post(LOGIN, json={"username": TEST_USER, "password": TEST_PASSWORD,
                                      "client": "ios"}).json()
    r = client.get(
        "/api/v1/stocks/2330.TW/dashboard",
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
    )
    assert r.status_code == 200


def test_refresh_token_cannot_be_used_as_access(client):
    tokens = client.post(LOGIN, json={"username": TEST_USER, "password": TEST_PASSWORD,
                                      "client": "ios"}).json()
    r = client.get(
        "/api/v1/stocks/2330.TW/dashboard",
        headers={"Authorization": f"Bearer {tokens['refresh_token']}"},
    )
    assert r.status_code == 401


def test_refresh_rotates_tokens(client):
    tokens = client.post(LOGIN, json={"username": TEST_USER, "password": TEST_PASSWORD,
                                      "client": "ios"}).json()
    r = client.post(
        "/api/v1/auth/refresh",
        headers={"Authorization": f"Bearer {tokens['refresh_token']}"},
    )
    assert r.status_code == 200
    assert r.json()["access_token"]


def test_logout_returns_204(web_client):
    r = web_client.post("/api/v1/auth/logout")
    assert r.status_code == 204
