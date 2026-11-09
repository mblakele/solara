"""Tests for OIDC authentication and the write gate.

Anonymous reads stay open; every non-read request must carry
``X-Requested-With: fetch`` and a session user whose (iss, sub) pair is on
the allowlist. The provider is faked: ``FakeOidcClient`` is injected into
``app.extensions["oidc_client"]`` after ``create_app()``, so no network or
real provider is involved.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

import pytest
from flask import Flask, redirect

import app as app_mod
from config import Config

ISSUER = "https://issuer.example.com"
SESSION_USER = {"iss": ISSUER, "sub": "sub-1", "email": "u@example.com"}
WRITE_PATH = "/api/v1/test-write"
WRITE_HEADERS = {"X-Requested-With": "fetch"}


class FakeOidcClient:
    """Test double for Authlib's remote app client.

    Records the redirect URI and returns canned claims, so the OIDC routes
    can be exercised without a real provider or network calls.
    """

    def __init__(self, claims: dict[str, Any] | None = None) -> None:
        self.claims = claims if claims is not None else dict(SESSION_USER)
        self.authorized_redirect_uri: str | None = None

    def authorize_redirect(self, redirect_uri: str | None = None, **kwargs: Any) -> Response:
        """Record the redirect URI and return a redirect to a fake provider."""
        self.authorized_redirect_uri = redirect_uri
        return redirect("https://issuer.example.com/authorize")

    def authorize_access_token(self) -> dict[str, str]:
        """Return a canned token payload (Authlib validates state internally)."""
        return {"access_token": "tok", "id_token": "idtok"}

    def parse_id_token(self, token: dict[str, str], claims: Any = None) -> dict[str, Any]:
        """Return the canned ID-token claims."""
        return dict(self.claims)


def _write_ok() -> tuple[str, int]:
    """Stand-in write handler proving the gate lets a permitted request through."""
    return "ok", 200


def _make_app(**overrides: str) -> Flask:
    """Build an app with OIDC wiring and one test-only write route."""
    base = {
        "OIDC_ISSUER": ISSUER,
        "OIDC_CLIENT_ID": "client-id",
        "OIDC_ALLOWED_SUB": f"{ISSUER}|sub-1",
        "MOCK": "True",
    }
    base.update(overrides)
    flask_app = app_mod.create_app(config=Config(overrides=base))
    flask_app.extensions["oidc_client"] = FakeOidcClient()
    flask_app.add_url_rule(WRITE_PATH, "test_write", _write_ok, methods=["POST"])
    return flask_app


@pytest.fixture
def client() -> Any:
    """Test client for an app with the fake OIDC client injected."""
    flask_app = _make_app()
    return flask_app.test_client()


def _login_as(flask_client: Any, user: dict[str, str] | None) -> None:
    """Store *user* (or no user) in the test client's session."""
    with flask_client.session_transaction() as sess:
        if user is None:
            sess.pop("user", None)
        else:
            sess["user"] = user


def test_anonymous_get_on_read_routes_works(client: Any) -> None:
    """Anonymous GETs on the read routes still work (gate only covers writes)."""
    assert client.get("/health").status_code == 200
    index = client.get("/", headers={"Accept": "application/json"})
    assert index.status_code == 200
    assert "devices" in index.get_json()


def test_anonymous_write_returns_401(client: Any) -> None:
    """Anonymous write (with the fetch header) is refused with 401."""
    resp = client.post(WRITE_PATH, headers=WRITE_HEADERS)
    assert resp.status_code == 401


def test_anonymous_write_without_header_returns_403(client: Any) -> None:
    """Write without the X-Requested-With header is refused with 403."""
    resp = client.post(WRITE_PATH)
    assert resp.status_code == 403


def test_logged_in_non_allowlisted_write_returns_403(client: Any) -> None:
    """Logged-in user whose (iss, sub) is not allowlisted is refused with 403."""
    _login_as(client, {"iss": ISSUER, "sub": "other", "email": "o@example.com"})
    resp = client.post(WRITE_PATH, headers=WRITE_HEADERS)
    assert resp.status_code == 403


def test_allowlisted_write_succeeds(client: Any) -> None:
    """Allowlisted user with the fetch header can write."""
    _login_as(client, SESSION_USER)
    resp = client.post(WRITE_PATH, headers=WRITE_HEADERS)
    assert resp.status_code == 200
    assert resp.data == b"ok"


def test_allowlisted_write_without_header_returns_403(client: Any) -> None:
    """Even an allowlisted user needs the X-Requested-With header."""
    _login_as(client, SESSION_USER)
    resp = client.post(WRITE_PATH)
    assert resp.status_code == 403


def test_empty_allowlist_denies_all_writes() -> None:
    """An empty OIDC_ALLOWED_SUB fails closed: nobody can write."""
    flask_app = _make_app(OIDC_ALLOWED_SUB="")
    test_client = flask_app.test_client()
    _login_as(test_client, SESSION_USER)
    resp = test_client.post(WRITE_PATH, headers=WRITE_HEADERS)
    assert resp.status_code == 403


def test_wrong_issuer_is_not_allowlisted() -> None:
    """Allowlist matches on (iss, sub): a foreign issuer's sub never matches."""
    flask_app = _make_app()
    test_client = flask_app.test_client()
    _login_as(
        test_client,
        {"iss": "https://evil.example.com", "sub": "sub-1", "email": "u@example.com"},
    )
    resp = test_client.post(WRITE_PATH, headers=WRITE_HEADERS)
    assert resp.status_code == 403


@pytest.mark.parametrize(
    "bad_next",
    ["//evil.com", "https://evil.com", "evil.com", ""],
)
def test_login_rejects_non_local_next(client: Any, bad_next: str) -> None:
    """A non-local `next` is replaced with "/" before it reaches the session."""
    resp = client.get(f"/oidc/login?next={bad_next}")
    assert resp.status_code == 302
    with client.session_transaction() as sess:
        assert sess.get("post_login_next") == "/"


def test_login_keeps_local_next(client: Any) -> None:
    """A local `next` path is stored verbatim for the post-login redirect."""
    resp = client.get("/oidc/login?next=/api/v1/tou")
    assert resp.status_code == 302
    with client.session_transaction() as sess:
        assert sess.get("post_login_next") == "/api/v1/tou"


def test_login_builds_redirect_uri_from_public_url() -> None:
    """The provider redirect URI is <PUBLIC_URL>/oidc/callback."""
    flask_app = _make_app(PUBLIC_URL="https://solara.example.com")
    flask_app.test_client().get("/oidc/login")
    fake = flask_app.extensions["oidc_client"]
    assert fake.authorized_redirect_uri == "https://solara.example.com/oidc/callback"


def test_callback_stores_user_and_redirects_to_next(client: Any) -> None:
    """Callback stores {iss, sub, email}, marks the session permanent, redirects."""
    with client.session_transaction() as sess:
        sess["post_login_next"] = "/api/v1/tou"
    resp = client.get("/oidc/callback")
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/api/v1/tou")
    with client.session_transaction() as sess:
        assert sess["user"] == SESSION_USER
        assert sess.permanent is True


def test_callback_defaults_to_root_next(client: Any) -> None:
    """Without a stored `next`, the callback redirects to "/"."""
    resp = client.get("/oidc/callback")
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/")


def test_callback_logs_iss_sub_when_allowlist_empty(caplog: Any) -> None:
    """With an empty allowlist the callback logs iss/sub at INFO for bootstrap."""
    flask_app = _make_app(OIDC_ALLOWED_SUB="")
    with caplog.at_level(logging.INFO, logger="oidc_auth"):
        flask_app.test_client().get("/oidc/callback")
    assert "iss=" in caplog.text


def test_logout_clears_session(client: Any) -> None:
    """Logout clears the session and redirects to "/"."""
    _login_as(client, SESSION_USER)
    resp = client.get("/oidc/logout")
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/")
    with client.session_transaction() as sess:
        assert "user" not in sess


def test_api_me_anonymous(client: Any) -> None:
    """Anonymous /api/me reports no user and no write permission."""
    resp = client.get("/api/me")
    assert resp.status_code == 200
    assert resp.get_json() == {"authenticated": False, "can_write": False}


def test_api_me_non_allowlisted(client: Any) -> None:
    """Logged-in but non-allowlisted /api/me reports authenticated, cannot write."""
    _login_as(client, {"iss": ISSUER, "sub": "other", "email": "o@example.com"})
    resp = client.get("/api/me")
    assert resp.get_json() == {"authenticated": True, "can_write": False}


def test_api_me_allowlisted(client: Any) -> None:
    """Allowlisted /api/me reports authenticated and can write."""
    _login_as(client, SESSION_USER)
    resp = client.get("/api/me")
    assert resp.get_json() == {"authenticated": True, "can_write": True}


def test_session_cookie_config() -> None:
    """Session cookies are HttpOnly, SameSite=Lax, 30-day permanent lifetime."""
    flask_app = _make_app()
    cfg = flask_app.config
    assert cfg["SESSION_COOKIE_HTTPONLY"] is True
    assert cfg["SESSION_COOKIE_SAMESITE"] == "Lax"
    assert cfg["PERMANENT_SESSION_LIFETIME"] == timedelta(days=30)
    # PUBLIC_URL defaults to http://localhost:8000, so Secure is off locally.
    assert cfg["SESSION_COOKIE_SECURE"] is False


def test_session_cookie_secure_when_public_url_is_https() -> None:
    """An https PUBLIC_URL turns SESSION_COOKIE_SECURE on by default."""
    flask_app = _make_app(PUBLIC_URL="https://solara.example.com")
    assert flask_app.config["SESSION_COOKIE_SECURE"] is True
