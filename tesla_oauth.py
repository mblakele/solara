"""Tesla OAuth flow endpoints.

Extracted from app.py to reduce its size and isolate the complex
async token exchange logic. Registered as a Flask Blueprint in app.py.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging

import html
import secrets
import threading
import time as _time_module
from datetime import datetime
from pathlib import Path

import pytz
from flask import Blueprint, abort, redirect, request, Response
from flask.typing import ResponseReturnValue

import config as _cfg_mod
from config_loader import load_tesla_config
from load_models import TeslaOwnerEmailError
from util import atomic_write_json

bp = Blueprint("tesla", __name__)
logger = logging.getLogger(__name__)


# State token tracking: maps state_token -> expiry timestamp.
_oauth_states: dict[str, float] = {}

TESLA_AUTH_TOKEN_FILE = Path(".tesla-auth-token.json")
TESLA_AUTH_TOKEN_TTL_SECS = 600

# Serializes generate and consume so two requests cannot both consume one
# token.
_TESLA_AUTH_TOKEN_LOCK = threading.Lock()


def _tesla_auth_token_hash(token: str) -> str:
    """Return the SHA-256 hex digest of *token*."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def generate_tesla_auth_token(
    path: Path = TESLA_AUTH_TOKEN_FILE, now: float | None = None
) -> tuple[str, float]:
    """Create a one-time initiate token and store only its hash.

    Args:
        path: Token file path.
        now: Current unix timestamp; defaults to the real clock.

    Returns:
        The plaintext token and its expiry timestamp.
    """
    current = _time_module.time() if now is None else now
    token = secrets.token_urlsafe(32)
    expires = current + TESLA_AUTH_TOKEN_TTL_SECS
    payload = {"token_sha256": _tesla_auth_token_hash(token), "expires": expires}
    with _TESLA_AUTH_TOKEN_LOCK:
        atomic_write_json(path, payload)
    return token, expires


def consume_tesla_auth_token(
    token: str, path: Path = TESLA_AUTH_TOKEN_FILE, now: float | None = None
) -> bool:
    """Atomically consume a valid, unexpired initiate token.

    Rejects a missing file, malformed JSON, a past expiry, or a hash
    mismatch. The file is deleted only after a successful match; a delete
    failure rejects the token.

    Args:
        token: Plaintext token from the ``token`` query parameter.
        path: Token file path.
        now: Current unix timestamp; defaults to the real clock.

    Returns:
        True when the token was valid and has now been consumed.
    """
    if not token:
        return False
    current = _time_module.time() if now is None else now
    with _TESLA_AUTH_TOKEN_LOCK:
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return False
        if not isinstance(data, dict):
            return False
        stored_hash = data.get("token_sha256")
        expires = data.get("expires")
        if not isinstance(stored_hash, str) or not isinstance(
            expires, (int, float)
        ):
            return False
        if current >= expires:
            return False
        if not secrets.compare_digest(
            stored_hash, _tesla_auth_token_hash(token)
        ):
            return False
        try:
            Path(path).unlink()
        except OSError as e:
            logger.error("Failed to consume Tesla auth token: %s", e)
            return False
        return True


def _controllers():
    """Import and return the load_controllers symbols used by the OAuth routes.

    Imported lazily so this module can be imported without aiohttp — the
    heavy controller machinery is only needed once a request handler runs.
    """
    from load_controllers import (
        RealTeslaController,
        load_tesla_tokens,
        save_tesla_tokens,
    )
    return RealTeslaController, load_tesla_tokens, save_tesla_tokens


def _json_response(payload: object, status: int = 200) -> Response:
    """Create a JSON response with proper content type header."""
    from flask import current_app

    resp = Response(current_app.json.dumps(payload))  # type: ignore[attr-defined]
    resp.headers["Content-Type"] = "application/json"
    resp.status_code = status
    return resp


def prune_expired_oauth_states(now: float | None = None) -> int:
    """Remove expired state tokens from ``_oauth_states``.

    Without this sweep the dict grows without bound: entries are added on
    every initiate and removed only when their callback arrives (R8).

    Args:
        now: Current unix timestamp; defaults to the real clock.

    Returns:
        Number of expired entries removed.
    """
    current = _time_module.time() if now is None else now
    # Snapshot the items with a single C-level list() call: iterating the
    # live dict through a Python comprehension can be suspended by a thread
    # switch while an /auth/initiate request inserts, raising
    # "dictionary changed size during iteration" (review #6). list() over a
    # dict view does not release the GIL, so the snapshot is atomic.
    expired = [
        key
        for key, expiry in list(_oauth_states.items())
        if expiry <= current
    ]
    removed = 0
    for key in expired:
        # pop (not del): a callback thread may have consumed this state
        # between the snapshot and now.
        if _oauth_states.pop(key, None) is not None:
            removed += 1
    return removed


@bp.route("/api/v1/tesla/auth/initiate")
def tesla_auth_initiate() -> ResponseReturnValue:
    """Initiate Tesla OAuth flow.

    Returns JSON with the authorization URL, or HTML with an auto-redirect
    when the client accepts text/html. The callback will be handled at /callback.
    """
    tesla_config = load_tesla_config()
    if tesla_config is None:
        return abort(503, "Tesla Fleet API not configured in .env")

    # A one-time token minted by --tesla-auth-url gates every initiation.
    # Consume it before the already-authenticated check: a valid token is one
    # use even when the response is "Already Authenticated".
    token = request.args.get("token", "")
    if not consume_tesla_auth_token(token, path=TESLA_AUTH_TOKEN_FILE):
        message = "Open the URL printed by --tesla-auth-url."
        if request.accept_mimetypes.accept_html:
            return (  # type: ignore[return-value]
                "<h1>Tesla Auth Required</h1>"
                f"<p>{html.escape(message)}</p>",
                403,
            )
        return _json_response(  # type: ignore[return-value]
            {"error": message}, status=403
        )

    # Check if already authenticated
    RealTeslaController, load_tesla_tokens, _ = _controllers()
    tokens = load_tesla_tokens()

    if tokens and tokens.get("expires", 0) > _time_module.time():
        valid_until = datetime.fromtimestamp(
            tokens["expires"], tz=pytz.UTC
        ).isoformat()
        if request.accept_mimetypes.accept_html:
            return (  # type: ignore[return-value]
                "<h1>Already Authenticated</h1>"
                f"<p>Tesla token valid until {html.escape(valid_until)}.</p>"
            )
        return _json_response({  # type: ignore[return-value]
            "authenticated": True,
            "message": f"Already authenticated. Token valid until: {valid_until}",
        })

    state_token = secrets.token_urlsafe(32)
    prune_expired_oauth_states()
    _oauth_states[state_token] = _time_module.time() + 600  # expire in 10 minutes

    try:
        controller = RealTeslaController(tesla_config)
        login_url = controller.get_login_url(state=state_token)
    except Exception as e:
        logger.error("Failed to generate Tesla login URL: %s", e)
        _oauth_states.pop(state_token, None)
        return abort(500, f"Failed to generate login URL: {e}")

    if request.accept_mimetypes.accept_html:
        return redirect(login_url)  # type: ignore[return-value]

    return _json_response({  # type: ignore[return-value]
        "authenticated": False,
        "loginUrl": login_url,
        "message": "Open this URL in your browser to authorize Tesla access.",
    })


@bp.route("/callback")
def tesla_auth_callback() -> ResponseReturnValue:
    """Handle OAuth callback from Tesla.

    Receives the authorization code, exchanges it for tokens, and persists them.
    Returns a success page on completion.
    """
    state = request.args.get("state", "")
    state_expiry = _oauth_states.pop(state, None)
    if not state or state_expiry is None or _time_module.time() > state_expiry:
        return (  # type: ignore[return-value]
            "<h1>Tesla Auth Failed</h1>"
            "<p>Invalid or expired state parameter. "
            "Please restart the authentication flow.</p>",
            400,
        )

    code = request.args.get("code")
    if not code:
        return (  # type: ignore[return-value]
            "<h1>Tesla Auth Failed</h1><p>No authorization code received. "
            "Please try again.</p>",
            400,
        )

    tesla_config = load_tesla_config()
    if tesla_config is None:
        return (  # type: ignore[return-value]
            "<h1>Tesla Auth Failed</h1><p>Tesla Fleet API not configured.</p>",
            503,
        )

    allowed_emails = _cfg_mod._config.tesla_owner_emails
    if not allowed_emails:
        logger.error(
            "Tesla OAuth callback rejected: TESLA_OWNER_EMAILS is empty"
        )
        return (  # type: ignore[return-value]
            "<h1>Tesla Auth Failed</h1>"
            "<p>TESLA_OWNER_EMAILS is not configured. Add your Tesla account "
            "email before authenticating.</p>",
            503,
        )

    async def _exchange() -> None:
        from load_controllers import (
            fetch_tesla_account_email,
            tesla_owner_email_allowed,
        )

        RealTeslaController, _, save_tesla_tokens = _controllers()
        controller = RealTeslaController(tesla_config)
        await controller.exchange_code(code)
        # pylint: disable=protected-access
        # RealTeslaController internals needed to persist OAuth tokens.
        await controller._ensure_api()  # type: ignore[attr-defined]
        assert controller._api is not None  # type: ignore[attr-defined]
        email = await fetch_tesla_account_email(
            controller._api  # type: ignore[attr-defined]
        )
        if not tesla_owner_email_allowed(email, allowed_emails):
            logger.warning(
                "Tesla OAuth rejected account email: %s", email
            )
            raise TeslaOwnerEmailError(
                "Tesla account email is not in TESLA_OWNER_EMAILS"
            )
        save_tesla_tokens(
            refresh_token=controller._api.refresh_token,  # type: ignore[attr-defined]
            access_token=controller._api._access_token,  # type: ignore[attr-defined]
            expires=controller._api.expires,  # type: ignore[attr-defined]
        )

    try:
        asyncio.run(_exchange())
    except TeslaOwnerEmailError:
        logger.warning(
            "Tesla OAuth rejected: account is not in TESLA_OWNER_EMAILS"
        )
        return (  # type: ignore[return-value]
            "<h1>Tesla Auth Failed</h1>"
            "<p>This Tesla account is not allowed to authenticate this "
            "deployment.</p>",
            403,
        )
    except Exception as e:
        logger.error("Tesla OAuth token exchange failed: %s", e)
        return (  # type: ignore[return-value]
            f"<h1>Tesla Auth Failed</h1>"
            f"<p>Token exchange error: {html.escape(str(e))}</p>",
            500,
        )

    return (  # type: ignore[return-value]
        "<h1>Tesla Authentication Successful!</h1>"
        "<p>You can close this tab or navigate away.</p>"
        "<p>Your Tesla is now configured for load management.</p>"
    )


@bp.route("/api/v1/tesla/status")
def tesla_status() -> Response:
    """Check Tesla authentication status.

    Returns whether a valid token exists and its expiration time.
    """
    tesla_config = load_tesla_config()
    if tesla_config is None:
        return _json_response({  # type: ignore[return-value]
            "configured": False,
            "authenticated": False,
            "message": "Tesla Fleet API not configured in .env",
        })

    _, load_tesla_tokens, _ = _controllers()
    tokens = load_tesla_tokens()
    if tokens is None:
        return _json_response({  # type: ignore[return-value]
            "configured": True,
            "authenticated": False,
            "message": "Not authenticated. Visit /api/v1/tesla/auth/initiate to begin.",
        })

    expired = tokens.get("expires", 0) <= _time_module.time()
    return _json_response({  # type: ignore[return-value]
        "configured": True,
        "authenticated": not expired,
        "tokenExpired": expired,
        "expiresAt": datetime.fromtimestamp(
            tokens["expires"], tz=pytz.UTC
        ).isoformat(),
    })
