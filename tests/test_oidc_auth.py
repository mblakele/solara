"""Tests for OIDC authentication and the write gate.

Anonymous reads stay open; every non-read request must carry
``X-Requested-With: fetch`` and a session user whose (iss, sub) pair is on
the allowlist. The provider is faked: ``FakeOidcClient`` is injected into
``app.extensions["oidc_client"]`` after ``create_app()``, so no network or
real provider is involved.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest
from flask import Flask, redirect

import app as app_mod
import oidc_auth
from config import Config
from oidc_auth import validate_next

ISSUER = "https://issuer.example.com"
SESSION_USER = {"iss": ISSUER, "sub": "sub-1", "email": "u@example.com"}
WRITE_PATH = "/api/v1/test-write"
WRITE_HEADERS = {"X-Requested-With": "fetch"}


class FakeOidcClient:
    """Test double for Authlib's remote app client.

    Records the redirect URI and returns canned claims, so the OIDC routes
    can be exercised without a real provider or network calls.
    """

    def __init__(
        self,
        claims: dict[str, Any] | None = None,
        *,
        include_userinfo: bool = True,
    ) -> None:
        self.claims = claims if claims is not None else dict(SESSION_USER)
        self.include_userinfo = include_userinfo
        self.authorized_redirect_uri: str | None = None
        self.authorized_params: dict[str, Any] = {}
        self.parse_id_token_calls = 0

    def authorize_redirect(
        self, redirect_uri: str | None = None, **kwargs: Any
    ) -> Response:
        """Record the redirect URI and extra params, return a fake provider redirect."""
        self.authorized_redirect_uri = redirect_uri
        self.authorized_params = dict(kwargs)
        return redirect("https://issuer.example.com/authorize")

    def authorize_access_token(self) -> dict[str, Any]:
        """Return a token shaped like Authlib's validated token payload.

        Authlib 1.9.4 validates state, nonce, signature, issuer, audience,
        and expiry here and stores the validated claims under ``userinfo``.
        """
        token: dict[str, Any] = {"access_token": "tok", "id_token": "idtok"}
        if self.include_userinfo:
            token["userinfo"] = dict(self.claims)
        return token

    def parse_id_token(
        self,
        token: Any,
        nonce: Any,
        claims_options: Any = None,
        claims_cls: Any = None,
        leeway: int = 120,
    ) -> Any:  # pylint: disable=unused-argument
        """Fail if the callback parses the ID token a second time."""
        self.parse_id_token_calls += 1
        raise AssertionError("callback must not re-parse the ID token")


def _write_ok() -> tuple[str, int]:
    """Stand-in write handler proving the gate lets a permitted request through."""
    return "ok", 200


def _make_app(**overrides: str) -> Flask:
    """Build an app with OIDC wiring and one test-only write route."""
    base = {
        "OIDC_ISSUER": ISSUER,
        "OIDC_CLIENT_ID": "client-id",
        "OIDC_ALLOWED_SUB": f"{ISSUER}|sub-1",
        "FLASK_SECRET_KEY": "test-secret-key",
        "MOCK": "True",
    }
    base.update(overrides)
    flask_app = app_mod.create_app(config=Config(overrides=base))
    flask_app.extensions["oidc_client"] = FakeOidcClient()
    flask_app.add_url_rule(WRITE_PATH, "test_write", _write_ok, methods=["POST"])
    return flask_app


def _make_local_app(**overrides: str) -> Flask:
    """Build an app with OIDC disabled, for the loopback read exception."""
    base = {
        "OIDC_ISSUER": "",
        "FLASK_SECRET_KEY": "test-secret-key",
        "MOCK": "True",
    }
    base.update(overrides)
    return app_mod.create_app(config=Config(overrides=base))


@pytest.fixture(autouse=True)
def _reset_auth_rate_limits() -> Iterator[None]:
    """Keep the per-process auth limiter from leaking between tests."""
    oidc_auth.reset_auth_rate_limits()
    yield
    oidc_auth.reset_auth_rate_limits()


@pytest.fixture
def client() -> Any:
    """Test client for an app with the fake OIDC client injected."""
    flask_app = _make_app()
    return flask_app.test_client()


def _signin_as(flask_client: Any, user: dict[str, str] | None) -> None:
    """Store *user* (or no user) in the test client's session."""
    with flask_client.session_transaction() as sess:
        if user is None:
            sess.pop("user", None)
        else:
            sess["user"] = user


def test_anonymous_html_read_redirects_to_signin(client: Any) -> None:
    """An anonymous HTML page read is sent to the provider with a local next."""
    resp = client.get("/", headers={"Accept": "text/html"})
    assert resp.status_code == 302
    location = resp.headers["Location"]
    assert location.startswith("/oidc/signin?next=")
    assert "%2F" in location


def test_public_paths_remain_anonymous(client: Any) -> None:
    """Health, /api/me, static assets, and the auth routes stay public."""
    assert client.get("/health").status_code == 200
    assert client.get("/api/me").status_code == 200
    assert client.get("/static/style.css").status_code == 200
    assert client.get("/oidc/signout").status_code == 200


def test_tesla_auth_paths_remain_public(client: Any) -> None:
    """Tesla OAuth stays reachable; only Tesla status is a gated data read."""
    initiate = client.get("/api/v1/tesla/auth/initiate")
    assert initiate.status_code != 401
    callback = client.get("/callback")
    assert callback.status_code != 401
    status = client.get("/api/v1/tesla/status")
    assert status.status_code == 401


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/api/v1/tou?start_date=2026-01-01&end_date=2026-01-01",
        "/api/v1/load/status",
        "/api/v1/metrics",
        "/api/v1/tesla/status",
    ],
)
def test_anonymous_json_read_returns_401(client: Any, path: str) -> None:
    """Anonymous JSON data reads fail closed with 401, not a redirect."""
    resp = client.get(path, headers={"Accept": "application/json"})
    assert resp.status_code == 401
    assert resp.is_json
    assert resp.get_json()["error"]


def test_anonymous_sse_returns_401(client: Any) -> None:
    """Anonymous SSE is refused with a plain 401 (EventSource cannot follow)."""
    resp = client.get("/stream/status", headers={"Accept": "text/event-stream"})
    assert resp.status_code == 401
    assert resp.mimetype == "text/plain"


def test_partial_fetch_returns_401_not_redirect(client: Any) -> None:
    """A partial fragment fetch is XHR/SSE traffic, so it gets JSON 401."""
    resp = client.get(
        "/?partial=metrics", headers={"Accept": "text/html"}
    )
    assert resp.status_code == 401
    assert resp.is_json


def test_allowlisted_read_succeeds(client: Any) -> None:
    """An allowlisted session can read data."""
    _signin_as(client, SESSION_USER)
    resp = client.get("/", headers={"Accept": "application/json"})
    assert resp.status_code == 200
    assert "devices" in resp.get_json()


def test_non_allowlisted_read_returns_403(client: Any) -> None:
    """An authenticated user who is not allowlisted is denied with 403."""
    _signin_as(client, {"iss": ISSUER, "sub": "other", "email": "o@example.com"})
    resp = client.get("/", headers={"Accept": "application/json"})
    assert resp.status_code == 403
    assert resp.is_json


def test_local_loopback_read_allowed_without_oidc() -> None:
    """Local development keeps anonymous reads when OIDC is not configured."""
    resp = _make_local_app().test_client().get(
        "/", headers={"Accept": "application/json"}
    )
    assert resp.status_code == 200


def test_non_loopback_read_denied_without_oidc() -> None:
    """A remote peer cannot use the loopback exception when OIDC is unset."""
    resp = _make_local_app().test_client().get(
        "/",
        headers={"Accept": "application/json"},
        environ_overrides={"REMOTE_ADDR": "203.0.113.5"},
    )
    assert resp.status_code == 503


def test_non_loopback_host_denied_without_oidc() -> None:
    """A non-local Host cannot use the loopback exception either."""
    resp = _make_local_app().test_client().get(
        "/",
        headers={"Accept": "application/json", "Host": "solara.example.com"},
    )
    assert resp.status_code == 503


def test_loopback_read_denied_when_oidc_configured(client: Any) -> None:
    """The loopback exception never applies once OIDC is configured."""
    resp = client.get("/", headers={"Accept": "application/json"})
    assert resp.status_code == 401


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
    _signin_as(client, {"iss": ISSUER, "sub": "other", "email": "o@example.com"})
    resp = client.post(WRITE_PATH, headers=WRITE_HEADERS)
    assert resp.status_code == 403


def test_allowlisted_write_succeeds(client: Any) -> None:
    """Allowlisted user with the fetch header can write."""
    _signin_as(client, SESSION_USER)
    resp = client.post(WRITE_PATH, headers=WRITE_HEADERS)
    assert resp.status_code == 200
    assert resp.data == b"ok"


def test_allowlisted_write_without_header_returns_403(client: Any) -> None:
    """Even an allowlisted user needs the X-Requested-With header."""
    _signin_as(client, SESSION_USER)
    resp = client.post(WRITE_PATH)
    assert resp.status_code == 403


def test_empty_allowlist_denies_all_writes() -> None:
    """An empty OIDC_ALLOWED_SUB fails closed: nobody can write."""
    flask_app = _make_app(OIDC_ALLOWED_SUB="")
    test_client = flask_app.test_client()
    _signin_as(test_client, SESSION_USER)
    resp = test_client.post(WRITE_PATH, headers=WRITE_HEADERS)
    assert resp.status_code == 403


def test_wrong_issuer_is_not_allowlisted() -> None:
    """Allowlist matches on (iss, sub): a foreign issuer's sub never matches."""
    flask_app = _make_app()
    test_client = flask_app.test_client()
    _signin_as(
        test_client,
        {"iss": "https://evil.example.com", "sub": "sub-1", "email": "u@example.com"},
    )
    resp = test_client.post(WRITE_PATH, headers=WRITE_HEADERS)
    assert resp.status_code == 403


@pytest.mark.parametrize(
    "bad_next",
    [
        "//evil.com",
        "https://evil.com",
        "evil.com",
        "",
        "/\\evil.com",
        "/%5Cevil.com",
        "/%2F%2Fevil.com",
        "/\t/evil.com",
        "/%0d%0aLocation:%20https://evil.com",
    ],
)
def test_validate_next_rejects_unsafe_values(bad_next: str) -> None:
    """Backslashes, encoded slashes, and control characters fall back to "/"."""
    assert validate_next(bad_next) == "/"


def test_validate_next_keeps_local_path_with_query() -> None:
    """A local path and its query string survive validation unchanged."""
    raw = "/api/v1/tou?start_date=2026-01-01&end_date=2026-01-02"
    assert validate_next(raw) == raw


@pytest.mark.parametrize(
    "issuer",
    ["http://issuer.example.com", "ftp://issuer.example.com", ""],
)
def test_issuer_requires_https(issuer: str) -> None:
    """A non-loopback issuer must use https."""
    assert oidc_auth._issuer_is_secure(issuer) is False


def test_issuer_accepts_https() -> None:
    """An https issuer is always accepted."""
    assert oidc_auth._issuer_is_secure(ISSUER) is True


@pytest.mark.parametrize(
    "issuer",
    [
        "http://localhost:8080",
        "http://127.0.0.1:9000",
        "http://[::1]:8000",
    ],
)
def test_issuer_allows_http_loopback(issuer: str) -> None:
    """Plain http is tolerated only for a loopback issuer (local provider)."""
    assert oidc_auth._issuer_is_secure(issuer) is True


def test_get_client_rejects_insecure_issuer(caplog: Any) -> None:
    """An insecure issuer cannot start the signin flow."""
    flask_app = _make_app(OIDC_ISSUER="http://issuer.example.com")
    flask_app.extensions.pop("oidc_client")
    with caplog.at_level(logging.ERROR, logger="oidc_auth"):
        resp = flask_app.test_client().get("/oidc/signin")
    assert resp.status_code == 503
    assert "https" in caplog.text
    assert "oidc_client" not in flask_app.extensions


def test_signin_rate_limit_returns_429(client: Any) -> None:
    """The 21st auth request in one window is refused with 429."""
    for _ in range(20):
        assert client.get("/oidc/signin").status_code == 302
    resp = client.get("/oidc/signin")
    assert resp.status_code == 429
    assert resp.headers["Retry-After"] == "60"
    assert resp.is_json


def test_signin_rate_limit_is_per_ip() -> None:
    """One client exhausting the window does not affect another IP."""
    test_client = _make_app().test_client()
    first = {"REMOTE_ADDR": "203.0.113.5"}
    second = {"REMOTE_ADDR": "203.0.113.6"}
    for _ in range(20):
        assert (
            test_client.get("/oidc/signin", environ_overrides=first).status_code
            == 302
        )
    assert (
        test_client.get("/oidc/signin", environ_overrides=first).status_code == 429
    )
    assert (
        test_client.get("/oidc/signin", environ_overrides=second).status_code
        == 302
    )


def test_signin_rate_limit_window_resets(client: Any, monkeypatch: Any) -> None:
    """A new fixed window allows requests from the same IP again."""
    clock = [1000.0]
    monkeypatch.setattr(oidc_auth, "_monotonic", lambda: clock[0])
    for _ in range(20):
        assert client.get("/oidc/signin").status_code == 302
    assert client.get("/oidc/signin").status_code == 429
    clock[0] += oidc_auth.AUTH_RATE_WINDOW_SECS + 1
    assert client.get("/oidc/signin").status_code == 302


@pytest.mark.parametrize(
    "bad_next",
    ["//evil.com", "https://evil.com", "evil.com", ""],
)
def test_signin_rejects_non_local_next(client: Any, bad_next: str) -> None:
    """A non-local `next` is replaced with "/" before it reaches the session."""
    resp = client.get(f"/oidc/signin?next={bad_next}")
    assert resp.status_code == 302
    with client.session_transaction() as sess:
        assert sess.get("post_signin_next") == "/"


def test_signin_keeps_local_next(client: Any) -> None:
    """A local `next` path is stored verbatim for the post-signin redirect."""
    resp = client.get("/oidc/signin?next=/api/v1/tou")
    assert resp.status_code == 302
    with client.session_transaction() as sess:
        assert sess.get("post_signin_next") == "/api/v1/tou"


def test_signin_builds_redirect_uri_from_public_url() -> None:
    """The provider redirect URI is <PUBLIC_URL>/oidc/callback."""
    flask_app = _make_app(PUBLIC_URL="https://solara.example.com")
    flask_app.test_client().get("/oidc/signin")
    fake = flask_app.extensions["oidc_client"]
    assert fake.authorized_redirect_uri == "https://solara.example.com/oidc/callback"


def test_callback_stores_user_and_redirects_to_next(client: Any) -> None:
    """Callback stores {iss, sub, email}, marks the session permanent, redirects."""
    with client.session_transaction() as sess:
        sess["post_signin_next"] = "/api/v1/tou"
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


def test_startup_logs_oidc_config_at_debug(caplog: Any) -> None:
    """Startup logs the issuer and parsed allowlist at DEBUG."""
    with caplog.at_level(logging.DEBUG, logger="oidc_auth"):
        _make_app()
    assert f"issuer={ISSUER}" in caplog.text
    assert f"{ISSUER}|sub-1" in caplog.text


def test_callback_logs_allowlist_mismatch_at_debug(caplog: Any) -> None:
    """A trailing-slash allowlist mismatch shows up at DEBUG on signin."""
    flask_app = _make_app(OIDC_ALLOWED_SUB=f"{ISSUER}/|sub-1")
    with caplog.at_level(logging.DEBUG, logger="oidc_auth"):
        flask_app.test_client().get("/oidc/callback")
    assert "allowlisted=False" in caplog.text
    assert f"iss={ISSUER}" in caplog.text


def test_denied_read_logs_identity_at_debug(client: Any, caplog: Any) -> None:
    """A non-allowlisted session logs its identity at DEBUG on denial."""
    _signin_as(client, {"iss": ISSUER, "sub": "other"})
    with caplog.at_level(logging.DEBUG, logger="oidc_auth"):
        resp = client.get("/api/v1/metrics")
    assert resp.status_code == 403
    assert "sub=other" in caplog.text
    assert "not in allowlist" in caplog.text


def test_anonymous_denial_logs_no_identity(client: Any, caplog: Any) -> None:
    """Anonymous denials stay quiet so unknown clients add no debug noise."""
    with caplog.at_level(logging.DEBUG, logger="oidc_auth"):
        resp = client.get("/api/v1/metrics")
    assert resp.status_code == 401
    assert "not in allowlist" not in caplog.text


def test_callback_uses_userinfo_from_authorize_access_token() -> None:
    """The callback trusts the claims Authlib already validated in the token."""
    flask_app = _make_app()
    flask_client = flask_app.test_client()
    resp = flask_client.get("/oidc/callback")
    assert resp.status_code == 302
    with flask_client.session_transaction() as sess:
        assert sess["user"] == SESSION_USER


def test_callback_rejects_missing_userinfo() -> None:
    """A token without validated userinfo fails closed with 502."""
    flask_app = _make_app()
    flask_app.extensions["oidc_client"] = FakeOidcClient(include_userinfo=False)
    flask_client = flask_app.test_client()
    resp = flask_client.get("/oidc/callback")
    assert resp.status_code == 502
    with flask_client.session_transaction() as sess:
        assert "user" not in sess


@pytest.mark.parametrize(
    "claims",
    [
        {"iss": ISSUER, "sub": 123},
        {"iss": None, "sub": "sub-1"},
        {"iss": ISSUER, "sub": ""},
        {"iss": "", "sub": "sub-1"},
    ],
)
def test_callback_rejects_non_string_iss_or_sub(claims: dict[str, Any]) -> None:
    """The callback stores no user unless iss and sub are non-empty strings."""
    flask_app = _make_app()
    flask_app.extensions["oidc_client"] = FakeOidcClient(claims=claims)
    flask_client = flask_app.test_client()
    resp = flask_client.get("/oidc/callback")
    assert resp.status_code == 502
    with flask_client.session_transaction() as sess:
        assert "user" not in sess


def test_callback_does_not_call_parse_id_token() -> None:
    """Authlib already validated the ID token; the callback must not parse it again."""
    flask_app = _make_app()
    resp = flask_app.test_client().get("/oidc/callback")
    assert resp.status_code == 302
    assert flask_app.extensions["oidc_client"].parse_id_token_calls == 0


def test_callback_omits_missing_email() -> None:
    """Email is optional; the stored user keeps only iss and sub without it."""
    flask_app = _make_app()
    flask_app.extensions["oidc_client"] = FakeOidcClient(
        claims={"iss": ISSUER, "sub": "sub-1"}
    )
    flask_client = flask_app.test_client()
    resp = flask_client.get("/oidc/callback")
    assert resp.status_code == 302
    with flask_client.session_transaction() as sess:
        assert sess["user"] == {"iss": ISSUER, "sub": "sub-1"}


def test_callback_omits_non_string_email() -> None:
    """A non-string email claim is not copied into the session."""
    flask_app = _make_app()
    flask_app.extensions["oidc_client"] = FakeOidcClient(
        claims={"iss": ISSUER, "sub": "sub-1", "email": 42}
    )
    flask_client = flask_app.test_client()
    resp = flask_client.get("/oidc/callback")
    assert resp.status_code == 302
    with flask_client.session_transaction() as sess:
        assert sess["user"] == {"iss": ISSUER, "sub": "sub-1"}


def test_signout_clears_session_and_renders_page(client: Any) -> None:
    """Signout clears the session and renders the landing page."""
    _signin_as(client, SESSION_USER)
    resp = client.get("/oidc/signout")
    assert resp.status_code == 200
    assert "Location" not in resp.headers
    assert "signed out" in resp.get_data(as_text=True).lower()
    with client.session_transaction() as sess:
        assert "user" not in sess


def test_signout_page_names_the_provider(client: Any) -> None:
    """The page says the provider session may still be active."""
    resp = client.get("/oidc/signout")
    body = resp.get_data(as_text=True)
    assert "issuer.example.com" in body
    assert "still be signed in" in body


def test_signout_page_omits_provider_when_oidc_is_off() -> None:
    """Without OIDC there is no provider session to explain."""
    resp = _make_local_app().test_client().get("/oidc/signout")
    assert resp.status_code == 200
    assert "issuer.example.com" not in resp.get_data(as_text=True)


SUBPATH_ENV = {"SCRIPT_NAME": "/solara"}


def test_signin_redirect_keeps_proxy_prefix() -> None:
    """A subpath mount redirects to <prefix>/oidc/signin with a prefixed next."""
    flask_client = _make_app().test_client()
    resp = flask_client.get(
        "/", headers={"Accept": "text/html"}, environ_overrides=SUBPATH_ENV
    )
    assert resp.status_code == 302
    location = resp.headers["Location"]
    assert location.startswith("/solara/oidc/signin?next=")
    assert "next=%2Fsolara%2F" in location


def test_signin_stores_prefixed_next_when_none_given() -> None:
    """A bare signin request stores the prefixed root as the post-signin target."""
    flask_client = _make_app().test_client()
    flask_client.get("/oidc/signin", environ_overrides=SUBPATH_ENV)
    with flask_client.session_transaction() as sess:
        assert sess["post_signin_next"] == "/solara/"


def test_callback_defaults_to_proxy_prefix() -> None:
    """Without a stored next, the callback returns to the prefixed root."""
    flask_client = _make_app().test_client()
    resp = flask_client.get("/oidc/callback", environ_overrides=SUBPATH_ENV)
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/solara/"


def test_signout_page_links_keep_proxy_prefix() -> None:
    """The signout page resolves its links under the proxy prefix."""
    flask_client = _make_app().test_client()
    _signin_as(flask_client, SESSION_USER)
    resp = flask_client.get("/oidc/signout", environ_overrides=SUBPATH_ENV)
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert 'href="/solara/oidc/signin"' in body
    assert 'href="/solara/static/style.css"' in body


def test_protocol_relative_prefix_cannot_poison_signout_page() -> None:
    """A forged prefix must not produce protocol-relative page links."""
    flask_client = _make_app().test_client()
    _signin_as(flask_client, SESSION_USER)
    resp = flask_client.get(
        "/oidc/signout", environ_overrides={"SCRIPT_NAME": "//evil.example.com"}
    )
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "//evil.example.com" not in body
    assert 'href="/oidc/signin"' in body


def test_proxy_fix_honours_forwarded_prefix() -> None:
    """PROXY_TRUSTED_HOPS wires X-Forwarded-Prefix into script_root."""
    flask_client = _make_app(PROXY_TRUSTED_HOPS="1").test_client()
    resp = flask_client.get(
        "/",
        headers={"Accept": "text/html", "X-Forwarded-Prefix": "/solara"},
    )
    assert resp.status_code == 302
    assert resp.headers["Location"].startswith("/solara/oidc/signin?next=")


def test_signin_request_has_no_prompt_by_default() -> None:
    """A normal signin lets the provider reuse its browser session."""
    flask_app = _make_app()
    flask_app.test_client().get("/oidc/signin")
    fake = flask_app.extensions["oidc_client"]
    assert "prompt" not in fake.authorized_params


def test_signout_forces_account_choice_on_next_signin_only() -> None:
    """An explicit signout makes the next signin ask for an account, once."""
    flask_app = _make_app()
    flask_client = flask_app.test_client()
    _signin_as(flask_client, SESSION_USER)
    fake = flask_app.extensions["oidc_client"]
    flask_client.get("/oidc/signout")
    flask_client.get("/oidc/signin")
    assert fake.authorized_params.get("prompt") == "select_account"
    fake.authorized_params.clear()
    flask_client.get("/oidc/signin")
    assert "prompt" not in fake.authorized_params


def test_api_me_anonymous(client: Any) -> None:
    """Anonymous /api/me reports no user, no access, and that signin is required."""
    resp = client.get("/api/me")
    assert resp.status_code == 200
    assert resp.get_json() == {
        "authenticated": False,
        "can_read": False,
        "can_write": False,
        "signin_required": True,
    }


def test_api_me_non_allowlisted(client: Any) -> None:
    """A signed-in non-allowlisted user gets 403, not a signin redirect loop."""
    _signin_as(client, {"iss": ISSUER, "sub": "other", "email": "o@example.com"})
    resp = client.get("/api/me")
    assert resp.get_json() == {
        "authenticated": True,
        "can_read": False,
        "can_write": False,
        "signin_required": False,
    }


def test_api_me_allowlisted(client: Any) -> None:
    """An allowlisted /api/me reports read and write access."""
    _signin_as(client, SESSION_USER)
    resp = client.get("/api/me")
    assert resp.get_json() == {
        "authenticated": True,
        "can_read": True,
        "can_write": True,
        "signin_required": False,
    }


def test_api_me_reports_can_read_and_signin_required() -> None:
    """Local anonymous reads report can_read without demanding a signin."""
    resp = _make_local_app().test_client().get("/api/me")
    assert resp.get_json() == {
        "authenticated": False,
        "can_read": True,
        "can_write": False,
        "signin_required": False,
    }


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


def test_oidc_configured_without_secret_key_fails_startup() -> None:
    """OIDC must not start with an empty session key (forgeable cookies)."""
    with pytest.raises(RuntimeError, match="FLASK_SECRET_KEY"):
        app_mod.create_app(
            config=Config(
                overrides={
                    "OIDC_ISSUER": ISSUER,
                    "OIDC_CLIENT_ID": "client-id",
                    "OIDC_ALLOWED_SUB": f"{ISSUER}|sub-1",
                    "FLASK_SECRET_KEY": "",
                    "MOCK": "True",
                }
            )
        )


def test_oidc_disabled_without_secret_key_uses_random_key() -> None:
    """Without OIDC, a missing key falls back to a generated per-process key."""
    flask_app = app_mod.create_app(
        config=Config(
            overrides={"OIDC_ISSUER": "", "FLASK_SECRET_KEY": "", "MOCK": "True"}
        )
    )
    assert flask_app.secret_key
    assert len(flask_app.secret_key) == 64


def test_oidc_uses_configured_secret_key() -> None:
    """A configured FLASK_SECRET_KEY is used verbatim for session signing."""
    flask_app = _make_app()
    assert flask_app.secret_key == "test-secret-key"


def test_oidc_never_uses_fixed_fallback_key() -> None:
    """The public dev key must never sign a session, even without OIDC."""
    overrides = {"OIDC_ISSUER": "", "FLASK_SECRET_KEY": "", "MOCK": "True"}
    first = app_mod.create_app(config=Config(overrides=overrides))
    second = app_mod.create_app(config=Config(overrides=overrides))
    for flask_app in (first, second):
        assert flask_app.secret_key != "dev-only-insecure-key"
    assert first.secret_key != second.secret_key


def test_proxy_hops_without_oidc_fails_startup() -> None:
    """Trusting proxy headers without OIDC lets a remote client spoof loopback."""
    with pytest.raises(RuntimeError, match="PROXY_TRUSTED_HOPS"):
        app_mod.create_app(
            config=Config(
                overrides={
                    "OIDC_ISSUER": "",
                    "FLASK_SECRET_KEY": "test-secret-key",
                    "PROXY_TRUSTED_HOPS": "1",
                    "MOCK": "True",
                }
            )
        )


def test_proxy_hops_with_oidc_warns_about_forwarded_headers(caplog: Any) -> None:
    """A trusted hop count is safe only if the proxy sets the forwarded headers."""
    with caplog.at_level(logging.WARNING):
        _make_app(PROXY_TRUSTED_HOPS="1")
    assert any(
        "X-Forwarded-For" in record.getMessage() for record in caplog.records
    )
