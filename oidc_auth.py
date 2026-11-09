"""OIDC browser-cookie authentication and the read/write gate.

Data reads require an allowlisted OIDC session when ``OIDC_ISSUER`` is
set. When OIDC is unconfigured, anonymous reads are allowed only for
loopback requests (both peer address and Host), which keeps local
development usable. Every write (any method other than GET, HEAD, or
OPTIONS) must arrive as a same-origin fetch and carry a session user
whose (iss, sub) pair is on the configured allowlist. The provider
handshake itself runs through Authlib (authorization code + PKCE
through OIDC discovery); this module only stores the validated claims
in the Flask session.

Routes (registered by :func:`register_oidc` at app build time):

- ``GET /oidc/login`` — validate ``next`` (local path only), store it in
  the session, and redirect to the provider.
- ``GET /oidc/callback`` — exchange the code, store {iss, sub, email} in
  the session, mark it permanent, and redirect to the stored ``next``.
- ``GET /oidc/logout`` — clear the session and redirect to ``/``.
- ``GET /api/me`` — {"authenticated": bool, "can_read": bool,
  "can_write": bool, "login_required": bool}; cosmetic only, the
  server-side gate is the real protection.

When the provider is not configured the login endpoint returns 503.
Writes always fail closed; reads fail closed with 503 for non-loopback
requests.
"""

from __future__ import annotations

import ipaddress
import logging
import secrets
import threading
import time
from datetime import timedelta
from typing import Any
from urllib.parse import unquote, urlencode, urlsplit

from flask import Flask, Response, abort, current_app, redirect, request, session
from flask.typing import ResponseReturnValue

from config import Config

logger = logging.getLogger(__name__)

# Endpoints that stay anonymous: health probes, auth handshakes, and static
# assets. Tesla OAuth is public because /initiate carries a one-time token and
# /callback is gated by its OAuth state plus the owner allowlist.
_PUBLIC_ENDPOINTS = {
    "static",
    "health",
    "api_me",
    "oidc_login",
    "oidc_callback",
    "oidc_logout",
    "tesla.tesla_auth_initiate",
    "tesla.tesla_auth_callback",
}

# Endpoints that render a full HTML page and can therefore redirect to login.
_HTML_PAGE_ENDPOINTS = {"index", "tou"}

# Methods that read rather than write.
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

# Same-origin fetch marker the gate requires on writes. A plain form POST
# (or curl) cannot set this header, which keeps the gate useful against
# cross-site forgery without a CSRF token.
_FETCH_HEADER = "X-Requested-With"
_FETCH_VALUE = "fetch"

PERMANENT_SESSION_LIFETIME = timedelta(days=30)

# In-memory fixed-window limit for the login handshake. It protects one
# process; a multi-worker deployment should also limit at the proxy.
AUTH_RATE_LIMIT = 20
AUTH_RATE_WINDOW_SECS = 60.0

_rate_limit_lock = threading.Lock()
_rate_limit_windows: dict[str, tuple[float, int]] = {}


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
    if not _issuer_is_secure(cfg.oidc_issuer):
        logger.error(
            "OIDC_ISSUER must use https (http is allowed only for loopback): %s",
            cfg.oidc_issuer,
        )
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


def _can_access() -> bool:
    """Return True when the session user is on the (iss, sub) allowlist.

    The same check governs reads and writes. An empty allowlist fails
    closed: nobody can read or write.
    """
    user = _get_user()
    if user is None:
        return False
    allowed = _request_config().oidc_allowed_subs
    if not allowed:
        return False
    return (user.get("iss"), user.get("sub")) in allowed


def _is_loopback_addr(addr: str | None) -> bool:
    """Return True when *addr* parses as a loopback IP address."""
    if not addr:
        return False
    try:
        return ipaddress.ip_address(addr).is_loopback
    except ValueError:
        return False


def _issuer_is_secure(issuer: str) -> bool:
    """Return True when the issuer is https, or http on a loopback host."""
    parts = urlsplit(issuer)
    if parts.scheme == "https":
        return True
    if parts.scheme != "http":
        return False
    hostname = (parts.hostname or "").lower()
    return hostname == "localhost" or _is_loopback_addr(hostname)


def _monotonic() -> float:
    """Return the current monotonic time (patchable in tests)."""
    return time.monotonic()


def _check_auth_rate_limit() -> Response | None:
    """Refuse the request when this IP exceeded the auth handshake window."""
    key = request.remote_addr or "unknown"
    now = _monotonic()
    with _rate_limit_lock:
        window_start, count = _rate_limit_windows.get(key, (now, 0))
        if now - window_start >= AUTH_RATE_WINDOW_SECS:
            window_start, count = now, 0
        count += 1
        _rate_limit_windows[key] = (window_start, count)
    if count <= AUTH_RATE_LIMIT:
        return None
    resp = _json_error("Too many authentication requests", 429)
    resp.headers["Retry-After"] = str(int(AUTH_RATE_WINDOW_SECS))
    return resp


def reset_auth_rate_limits() -> None:
    """Clear the in-process auth limiter (tests, or an operator reset)."""
    with _rate_limit_lock:
        _rate_limit_windows.clear()


def _request_hostname() -> str | None:
    """Return the request Host without its port, lowercased."""
    host = request.host
    if not host:
        return None
    try:
        hostname = urlsplit(f"//{host}").hostname
    except ValueError:
        return None
    return hostname.lower() if hostname else None


def _local_anonymous_read_allowed() -> bool:
    """Return True when this anonymous request may read without OIDC.

    Only when OIDC is unconfigured and both the peer address and the Host
    are loopback. This keeps local development usable without weakening a
    deployment: once OIDC is configured the exception never applies.
    """
    if _request_config().oidc_issuer:
        return False
    hostname = _request_hostname()
    host_is_loopback = hostname == "localhost" or _is_loopback_addr(hostname)
    return _is_loopback_addr(request.remote_addr) and host_is_loopback


def _can_read() -> bool:
    """Return True when this request may read data."""
    return _can_access() or _local_anonymous_read_allowed()


def _login_required() -> bool:
    """Return True when logging in could grant this request read access.

    A non-allowlisted session gets 403 instead of a redirect, so this is
    true only for anonymous requests while OIDC is configured.
    """
    return (
        _request_config().oidc_issuer is not None
        and _get_user() is None
        and not _can_read()
    )


def _wants_html_page() -> bool:
    """Return True when the client asked for a full HTML page, not data."""
    return (
        request.endpoint in _HTML_PAGE_ENDPOINTS
        and request.args.get("partial") is None
        and request.headers.get(_FETCH_HEADER) != _FETCH_VALUE
        and request.accept_mimetypes.accept_html
    )


def _json_response(payload: dict[str, Any], status: int = 200) -> Response:
    """Return *payload* as a JSON response."""
    return Response(
        current_app.json.dumps(payload), status=status, mimetype="application/json"
    )


def _json_error(message: str, status: int) -> Response:
    """Return a JSON error response."""
    return _json_response({"error": message}, status=status)


def _deny_read(status: int, message: str) -> Response:
    """Return a read denial in the shape this client can consume."""
    if request.endpoint == "stream_status":
        return Response(message + "\n", status=status, mimetype="text/plain")
    if _wants_html_page():
        abort(status, message)
    return _json_error(message, status)


def _login_redirect() -> Response:
    """Send a browser page request to the provider with a local ``next``."""
    target = request.script_root + request.full_path
    if target.endswith("?"):
        target = target[:-1]
    login_url = f"{request.script_root}/oidc/login"
    return redirect(f"{login_url}?{urlencode({'next': target})}")  # type: ignore[return-value]


def validate_next(raw: str | None) -> str:
    """Return *raw* when it is a local path, else ``"/"``.

    A valid ``next`` starts with ``/`` but not ``//`` (a protocol-relative
    URL such as ``//evil.com``). Absolute URLs are rejected too. Encoded
    backslashes, encoded ``//`` prefixes, and control characters are
    rejected after one round of percent-decoding, because browsers and
    proxies disagree about which form they normalize.
    """
    if raw is None:
        return "/"
    raw = raw.strip()
    if not raw or not raw.startswith("/") or raw.startswith("//"):
        return "/"
    decoded = unquote(raw)
    if decoded.startswith("//"):
        return "/"
    if "\\" in decoded:
        return "/"
    if any(ord(char) < 32 or ord(char) == 127 for char in decoded):
        return "/"
    parts = urlsplit(raw)
    if parts.scheme or parts.netloc:
        return "/"
    return raw


def login() -> ResponseReturnValue:
    """Redirect the browser to the OIDC provider.

    The requested ``next`` path (local only) is stored in the session so
    the callback can return the user there after login.
    """
    limited = _check_auth_rate_limit()
    if limited is not None:
        return limited
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
    limited = _check_auth_rate_limit()
    if limited is not None:
        return limited
    client = _get_client()
    if client is None:
        abort(503, "OIDC login is not configured")
    try:
        token = client.authorize_access_token()
    except Exception:  # pylint: disable=broad-exception-caught
        logger.error("OIDC token exchange or ID token validation failed", exc_info=True)
        abort(502, "OIDC login failed")
    claims = token.get("userinfo") if isinstance(token, dict) else None
    if not isinstance(claims, dict):
        logger.error("OIDC login failed: token has no validated userinfo")
        abort(502, "OIDC login failed")
    iss = claims.get("iss")
    sub = claims.get("sub")
    if not isinstance(iss, str) or not iss or not isinstance(sub, str) or not sub:
        logger.error("OIDC login failed: token claims lack string iss/sub")
        abort(502, "OIDC login failed")
    user = {"iss": iss, "sub": sub}
    email = claims.get("email")
    if isinstance(email, str):
        user["email"] = email
    session["user"] = user
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
    """Return the current access state for the UI (cosmetic only)."""
    user = _get_user()
    payload = {
        "authenticated": user is not None,
        "can_read": _can_read(),
        "can_write": _can_access(),
        "login_required": _login_required(),
    }
    return _json_response(payload)


def _gate_reads() -> Response | None:
    """Deny data reads without an allowlisted session or a local exception."""
    if request.endpoint in _PUBLIC_ENDPOINTS:
        return None
    if _can_read():
        return None
    cfg = _request_config()
    if not cfg.oidc_issuer:
        return _deny_read(
            503, "OIDC is not configured; anonymous reads are local-only"
        )
    if _get_user() is not None:
        return _deny_read(403, "Not allowed to read")
    if _wants_html_page():
        return _login_redirect()
    return _deny_read(401, "Authentication required")


def _gate_requests() -> Response | None:
    """Deny unauthenticated or non-allowlisted reads and writes.

    Runs as a ``before_request`` hook. Public endpoints pass through. Safe
    methods use the read gate. Every other method needs the fetch marker
    (403), a session (401), and an allowlist match (403), so a non-fetch
    request never reaches a write handler.
    """
    if request.method in _SAFE_METHODS:
        return _gate_reads()
    if request.endpoint in _PUBLIC_ENDPOINTS:
        return None
    if request.headers.get(_FETCH_HEADER) != _FETCH_VALUE:
        abort(403, "Write requests require X-Requested-With: fetch")
    if _get_user() is None:
        abort(401, "Authentication required")
    if not _can_access():
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
    application.before_request(_gate_requests)
    application.config["SESSION_COOKIE_HTTPONLY"] = True
    application.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    application.config["PERMANENT_SESSION_LIFETIME"] = PERMANENT_SESSION_LIFETIME
    cfg = application.config.get("SOLARA_CONFIG")
    if isinstance(cfg, Config):
        secret_key = cfg.flask_secret_key
        if not secret_key:
            if cfg.oidc_issuer:
                raise RuntimeError(
                    "FLASK_SECRET_KEY must be set when OIDC_ISSUER is configured; "
                    "an empty key allows session forgery"
                )
            secret_key = secrets.token_hex(32)
            logger.warning(
                "FLASK_SECRET_KEY is unset; using a random per-process session key"
            )
        application.secret_key = secret_key
        application.config["SESSION_COOKIE_SECURE"] = cfg.session_cookie_secure
