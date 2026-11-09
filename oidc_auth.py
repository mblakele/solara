"""OIDC browser-cookie authentication and the write gate.

Anonymous users keep full read access. Every write (any method other than
GET, HEAD, or OPTIONS) must arrive as a same-origin fetch and carry a
session user whose (iss, sub) pair is on the configured allowlist. The
provider handshake itself runs through Authlib (authorization code + PKCE
through OIDC discovery); this module only stores the validated claims in
the Flask session.

Routes (registered by :func:`register_oidc` at app build time):

- ``GET /oidc/login`` — validate ``next`` (local path only), store it in
  the session, and redirect to the provider.
- ``GET /oidc/callback`` — exchange the code, store {iss, sub, email} in
  the session, mark it permanent, and redirect to the stored ``next``.
- ``GET /oidc/logout`` — clear the session and redirect to ``/``.
- ``GET /api/me`` — {"authenticated": bool, "can_write": bool}; cosmetic
  only, the server-side gate is the real protection.

When the provider is not configured the login endpoint returns 503 and the
gate still fails closed for writes; reads are unaffected.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from flask import Flask, Response, abort, current_app, redirect, request, session
from flask.typing import ResponseReturnValue

from config import Config

logger = logging.getLogger(__name__)

# Endpoints the gate always lets through (login/logout builtins).
_ANON_PATHS = {"/oidc/login", "/oidc/callback", "/oidc/logout"}

# Methods that never require login.
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

# Same-origin fetch marker the gate requires on writes. A plain form POST
# (or curl) cannot set this header, which keeps the gate useful against
# cross-site forgery without a CSRF token.
_FETCH_HEADER = "X-Requested-With"
_FETCH_VALUE = "fetch"

PERMANENT_SESSION_LIFETIME = timedelta(days=30)


def _request_config() -> Config:
    """Return the config bound to the active app, or the process singleton."""
    cfg = current_app.config.get("SOLARA_CONFIG")
    if isinstance(cfg, Config):
        return cfg
    from config import _config as fallback

    return fallback


def _get_client() -> Any:
    """Return the OIDC client for the active app, creating it when configured.

    The client is cached on ``app.extensions["oidc_client"]`` so Authlib
    keeps its OIDC metadata cache across requests. Tests inject a fake by
    assigning to the same key before the first request.
    """
    client = current_app.extensions.get("oidc_client")
    if client is not None:
        return client
    cfg = _request_config()
    if not cfg.oidc_issuer:
        return None
    from authlib.integrations.flask_client import OAuth

    oauth = current_app.extensions.get("oauth")
    if oauth is None:
        oauth = OAuth(current_app._get_current_object())  # type: ignore[attr-defined]
        current_app.extensions["oauth"] = oauth
    client = oauth.register(
        "oidc",
        server_metadata_url=f"{cfg.oidc_issuer.rstrip('/')}/.well-known/openid-configuration",
        client_id=cfg.oidc_client_id,
        client_secret=cfg.oidc_client_secret,
        redirect_uri=f"{cfg.public_url.rstrip('/')}/oidc/callback",
        client_kwargs={"scope": "openid email"},
    )
    current_app.extensions["oidc_client"] = client
    return client


def _get_user() -> dict[str, Any] | None:
    """Return the session user dict, or None for anonymous requests."""
    user = session.get("user")
    return user if isinstance(user, dict) else None


def _can_write() -> bool:
    """Return True when the session user is on the (iss, sub) allowlist.

    An empty allowlist fails closed: nobody can write.
    """
    user = _get_user()
    if user is None:
        return False
    allowed = _request_config().oidc_allowed_subs
    if not allowed:
        return False
    return (user.get("iss"), user.get("sub")) in allowed


def validate_next(raw: str | None) -> str:
    """Return *raw* when it is a local path, else ``"/"``.

    A valid ``next`` starts with ``/`` but not ``//`` (a protocol-relative
    URL such as ``//evil.com``). Absolute URLs are rejected too.
    """
    if raw is None:
        return "/"
    raw = raw.strip()
    if not raw or not raw.startswith("/") or raw.startswith("//"):
        return "/"
    return raw


def login() -> ResponseReturnValue:
    """Redirect the browser to the OIDC provider.

    The requested ``next`` path (local only) is stored in the session so
    the callback can return the user there after login.
    """
    client = _get_client()
    if client is None:
        abort(503, "OIDC login is not configured")
    session["post_login_next"] = validate_next(request.args.get("next"))
    redirect_uri = f"{_request_config().public_url.rstrip('/')}/oidc/callback"
    return client.authorize_redirect(redirect_uri)  # type: ignore[return-value]


def callback() -> ResponseReturnValue:
    """Complete the OIDC login and store the identity in the session.

    Authlib validates state, nonce, and the ID token. Failures abort with
    502 rather than letting an unverified identity into the session. When
    the allowlist is empty (initial bootstrap), the authenticated iss/sub
    are logged at INFO so they can be copied into configuration; writes
    stay denied until the allowlist is set.
    """
    client = _get_client()
    if client is None:
        abort(503, "OIDC login is not configured")
    try:
        token = client.authorize_access_token()
        claims = client.parse_id_token(token)
    except Exception:  # pylint: disable=broad-exception-caught
        logger.error("OIDC token exchange or ID token validation failed", exc_info=True)
        abort(502, "OIDC login failed")
    iss = claims.get("iss")
    sub = claims.get("sub")
    email = claims.get("email")
    session["user"] = {"iss": iss, "sub": sub, "email": email}
    session.permanent = True
    if not _request_config().oidc_allowed_subs:
        logger.info(
            "OIDC login bootstrap: iss=%s sub=%s not yet allowlisted; writes denied",
            iss,
            sub,
        )
    next_path = validate_next(session.pop("post_login_next", None))
    return redirect(next_path)  # type: ignore[return-value]


def logout() -> ResponseReturnValue:
    """Clear the session and redirect to the dashboard."""
    session.clear()
    return redirect("/")  # type: ignore[return-value]


def me() -> Response:
    """Return the current login state for the UI (cosmetic only)."""
    user = _get_user()
    payload = {
        "authenticated": user is not None,
        "can_write": _can_write(),
    }
    resp = Response(current_app.json.dumps(payload))
    resp.headers["Content-Type"] = "application/json"
    return resp


def _gate_writes() -> Response | None:
    """Deny unauthenticated or non-allowlisted writes.

    Runs as a ``before_request`` hook. Order matters: safe methods and the
    auth endpoints pass first; then the fetch header check (403), the
    session check (401), and finally the allowlist check (403), so a
    non-fetch request never reaches the write handler.
    """
    if request.method in _SAFE_METHODS:
        return None
    if request.endpoint in {"oidc_login", "oidc_callback", "oidc_logout", "api_me"}:
        return None
    if request.path in _ANON_PATHS:
        return None
    if request.headers.get(_FETCH_HEADER) != _FETCH_VALUE:
        abort(403, "Write requests require X-Requested-With: fetch")
    if _get_user() is None:
        abort(401, "Authentication required")
    if not _can_write():
        abort(403, "Not allowed to write")
    return None


def register_oidc(application: Flask) -> None:
    """Register the OIDC routes, the write gate, and session cookie config.

    Args:
        application: The Flask app being built (from ``create_app()``).
    """
    application.route("/oidc/login", endpoint="oidc_login")(login)
    application.route("/oidc/callback", endpoint="oidc_callback")(callback)
    application.route("/oidc/logout", endpoint="oidc_logout")(logout)
    application.route("/api/me", endpoint="api_me", methods=["GET"])(me)
    application.before_request(_gate_writes)
    application.config["SESSION_COOKIE_HTTPONLY"] = True
    application.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    application.config["PERMANENT_SESSION_LIFETIME"] = PERMANENT_SESSION_LIFETIME
    cfg = application.config.get("SOLARA_CONFIG")
    if isinstance(cfg, Config):
        application.secret_key = cfg.flask_secret_key or "dev-only-insecure-key"
        application.config["SESSION_COOKIE_SECURE"] = cfg.session_cookie_secure
