"""Tests for Tesla OAuth routes and one-time initiation tokens."""

from __future__ import annotations

import hashlib
import json
import time
from unittest.mock import AsyncMock, MagicMock

import tesla_oauth
from app import _print_tesla_auth_url, app
from config import Config
from tesla_oauth import (
    TESLA_AUTH_TOKEN_TTL_SECS,
    consume_tesla_auth_token,
    generate_tesla_auth_token,
)
from tests.helpers import make_tesla_config


class _FakeTeslaController:
    """Minimal RealTeslaController stand-in for the OAuth callback."""

    email = "owner@example.com"

    def __init__(self, config):
        self.config = config
        self._api = MagicMock()
        self._api.refresh_token = "refresh"
        self._api._access_token = "access"
        self._api.expires = 12345
        self._api.user.me = AsyncMock(return_value={"email": self.email})

    async def exchange_code(self, code):
        self.code = code

    async def _ensure_api(self):
        pass


def _patch_callback(monkeypatch, email, save_tokens):
    """Patch the callback's controller factory and token saver."""
    fake_cls = type("_Fake", (_FakeTeslaController,), {"email": email})
    monkeypatch.setattr(
        tesla_oauth,
        "_controllers",
        lambda: (fake_cls, lambda: None, save_tokens),
    )
    monkeypatch.setattr(
        tesla_oauth, "load_tesla_config", lambda: make_tesla_config()
    )


def _set_state():
    """Install one valid OAuth state entry."""
    tesla_oauth._oauth_states.clear()
    tesla_oauth._oauth_states["state"] = time.time() + 600


def test_callback_rejects_non_owner_email(monkeypatch):
    """A callback from a non-allowlisted account saves nothing."""
    save_tokens = MagicMock()
    _patch_callback(monkeypatch, "attacker@example.com", save_tokens)
    monkeypatch.setenv("TESLA_OWNER_EMAILS", "owner@example.com")
    _set_state()

    response = app.test_client().get("/callback?state=state&code=abc")

    assert response.status_code == 403
    save_tokens.assert_not_called()


def test_callback_saves_tokens_for_owner_email(monkeypatch):
    """A callback from an allowlisted account saves tokens."""
    save_tokens = MagicMock()
    _patch_callback(monkeypatch, "owner@example.com", save_tokens)
    monkeypatch.setenv("TESLA_OWNER_EMAILS", "owner@example.com")
    _set_state()

    response = app.test_client().get("/callback?state=state&code=abc")

    assert response.status_code == 200
    save_tokens.assert_called_once()


def test_callback_rejects_when_allowlist_empty(monkeypatch):
    """An empty allowlist rejects authentication before the exchange."""
    save_tokens = MagicMock()
    _patch_callback(monkeypatch, "owner@example.com", save_tokens)
    monkeypatch.setenv("TESLA_OWNER_EMAILS", "")
    _set_state()

    response = app.test_client().get("/callback?state=state&code=abc")

    assert response.status_code == 503
    save_tokens.assert_not_called()


# =============================================================================
# One-time initiate token store
# =============================================================================


def test_generate_token_writes_hash_only(tmp_path):
    """The token file never contains the plaintext token."""
    path = tmp_path / "token.json"

    token, expires = generate_tesla_auth_token(path=path, now=1000.0)

    raw = path.read_text()
    assert token not in raw
    data = json.loads(raw)
    assert data == {
        "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
        "expires": 1000.0 + TESLA_AUTH_TOKEN_TTL_SECS,
    }
    assert expires == 1000.0 + TESLA_AUTH_TOKEN_TTL_SECS


def test_consume_token_accepts_valid_token(tmp_path):
    """A freshly generated token is accepted."""
    path = tmp_path / "token.json"
    token, _ = generate_tesla_auth_token(path=path, now=1000.0)

    assert consume_tesla_auth_token(token, path=path, now=1001.0) is True


def test_consume_token_rejects_wrong_token(tmp_path):
    """A bad token is rejected and leaves the real token usable."""
    path = tmp_path / "token.json"
    token, _ = generate_tesla_auth_token(path=path, now=1000.0)

    assert consume_tesla_auth_token("not-the-token", path=path, now=1001.0) is False
    assert consume_tesla_auth_token(token, path=path, now=1002.0) is True


def test_consume_token_rejects_expired_token(tmp_path):
    """A token at or past its expiry is rejected."""
    path = tmp_path / "token.json"
    token, expires = generate_tesla_auth_token(path=path, now=1000.0)

    assert consume_tesla_auth_token(token, path=path, now=expires) is False


def test_consume_token_rejects_malformed_file(tmp_path):
    """A malformed or missing token file fails closed."""
    path = tmp_path / "token.json"
    token, _ = generate_tesla_auth_token(path=path, now=1000.0)
    path.write_text("not json")

    assert consume_tesla_auth_token(token, path=path, now=1001.0) is False
    assert consume_tesla_auth_token(token, path=tmp_path / "missing", now=1001.0) is False


def test_consume_token_is_one_time(tmp_path):
    """The same token cannot be consumed twice."""
    path = tmp_path / "token.json"
    token, _ = generate_tesla_auth_token(path=path, now=1000.0)

    assert consume_tesla_auth_token(token, path=path, now=1001.0) is True
    assert consume_tesla_auth_token(token, path=path, now=1002.0) is False


def test_generate_token_replaces_previous_token(tmp_path):
    """Generating a new token invalidates the old one."""
    path = tmp_path / "token.json"
    first, _ = generate_tesla_auth_token(path=path, now=1000.0)
    second, _ = generate_tesla_auth_token(path=path, now=1001.0)

    assert consume_tesla_auth_token(first, path=path, now=1002.0) is False
    assert consume_tesla_auth_token(second, path=path, now=1003.0) is True


def test_tesla_auth_url_cli_prints_url(tmp_path, capsys):
    """The CLI prints the public initiate URL and the owner warning."""
    cfg = Config(
        overrides={
            "PUBLIC_URL": "https://solar.example.com",
            "TESLA_OWNER_EMAILS": "",
        }
    )
    path = tmp_path / "token.json"

    _print_tesla_auth_url(cfg, token_path=path)

    out = capsys.readouterr().out
    assert "https://solar.example.com/api/v1/tesla/auth/initiate?token=" in out
    assert "TESLA_OWNER_EMAILS" in out
    assert path.exists()


# =============================================================================
# /initiate gate
# =============================================================================


class _FakeInitiateController:
    """Minimal controller factory for the initiate route."""

    def __init__(self, config):
        self.config = config

    def get_login_url(self, state):
        return f"https://auth.tesla.com/login?state={state}"


def _patch_initiate(monkeypatch, tmp_path, tokens=None):
    """Patch the initiate route's config, controller, and token file."""
    monkeypatch.setattr(
        tesla_oauth, "TESLA_AUTH_TOKEN_FILE", tmp_path / "token.json"
    )
    monkeypatch.setattr(
        tesla_oauth, "load_tesla_config", lambda: make_tesla_config()
    )
    monkeypatch.setattr(
        tesla_oauth,
        "_controllers",
        lambda: (_FakeInitiateController, lambda: tokens, MagicMock()),
    )


def _new_token(tmp_path):
    """Generate a token that the initiate route will read."""
    token, _ = generate_tesla_auth_token(path=tmp_path / "token.json")
    return token


def test_initiate_requires_token(monkeypatch, tmp_path):
    """An initiate request without a token is rejected."""
    _patch_initiate(monkeypatch, tmp_path)

    response = app.test_client().get("/api/v1/tesla/auth/initiate")

    assert response.status_code == 403


def test_initiate_rejects_invalid_token(monkeypatch, tmp_path):
    """A wrong token is rejected."""
    _patch_initiate(monkeypatch, tmp_path)
    _new_token(tmp_path)

    response = app.test_client().get(
        "/api/v1/tesla/auth/initiate?token=wrong"
    )

    assert response.status_code == 403


def test_initiate_accepts_valid_token_and_creates_state(
    monkeypatch, tmp_path
):
    """A valid token creates one OAuth state entry."""
    _patch_initiate(monkeypatch, tmp_path)
    token = _new_token(tmp_path)
    tesla_oauth._oauth_states.clear()

    response = app.test_client().get(
        f"/api/v1/tesla/auth/initiate?token={token}"
    )

    assert response.status_code == 200
    assert len(tesla_oauth._oauth_states) == 1


def test_initiate_consumes_token(monkeypatch, tmp_path):
    """The token cannot start a second flow."""
    _patch_initiate(monkeypatch, tmp_path)
    token = _new_token(tmp_path)

    first = app.test_client().get(
        f"/api/v1/tesla/auth/initiate?token={token}"
    )
    second = app.test_client().get(
        f"/api/v1/tesla/auth/initiate?token={token}"
    )

    assert first.status_code == 200
    assert second.status_code == 403


def test_initiate_json_response_is_403(monkeypatch, tmp_path):
    """A non-HTML client gets a JSON 403 body."""
    _patch_initiate(monkeypatch, tmp_path)

    response = app.test_client().get(
        "/api/v1/tesla/auth/initiate",
        headers={"Accept": "application/json"},
    )

    assert response.status_code == 403
    assert response.is_json
    assert "--tesla-auth-url" in response.get_json()["error"]
