"""Tesla OAuth flow endpoints.

Extracted from app.py to reduce its size and isolate the complex
async token exchange logic. Registered as a Flask Blueprint in app.py.
"""

from __future__ import annotations

import asyncio
import logging

import html
import secrets
import time as _time_module
from datetime import datetime

import pytz
from flask import Blueprint, abort, redirect, request, Response
from flask.typing import ResponseReturnValue

bp = Blueprint("tesla", __name__)
logger = logging.getLogger(__name__)


# State token tracking: maps state_token -> expiry timestamp.
_oauth_states: dict[str, float] = {}


def _json_response(payload: object) -> Response:
    """Create a JSON response with proper content type header."""
    from flask import current_app

    resp = Response(current_app.json.dumps(payload))  # type: ignore[attr-defined]
    resp.headers["Content-Type"] = "application/json"
    return resp


@bp.route("/api/v1/tesla/auth/initiate")
def tesla_auth_initiate() -> ResponseReturnValue:
    """Initiate Tesla OAuth flow.

    Returns JSON with the authorization URL, or HTML with an auto-redirect
    when the client accepts text/html. The callback will be handled at /callback.
    """
    from load_manager import (  # noqa: PLC0415
        RealTeslaController,
        load_tesla_config,
        load_tesla_tokens,
    )

    tesla_config = load_tesla_config()
    if tesla_config is None:
        return abort(503, "Tesla Fleet API not configured in .env")

    # Check if already authenticated
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
    from load_manager import (  # noqa: PLC0415
        RealTeslaController,
        save_tesla_tokens,
        load_tesla_config,
    )

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

    async def _exchange() -> None:
        controller = RealTeslaController(tesla_config)
        await controller.exchange_code(code)
        # pylint: disable=protected-access
        # RealTeslaController internals needed to persist OAuth tokens.
        await controller._ensure_api()  # type: ignore[attr-defined]
        assert controller._api is not None  # type: ignore[attr-defined]
        save_tesla_tokens(
            refresh_token=controller._api.refresh_token,  # type: ignore[attr-defined]
            access_token=controller._api._access_token,  # type: ignore[attr-defined]
            expires=controller._api.expires,  # type: ignore[attr-defined]
        )

    try:
        asyncio.run(_exchange())
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
    from load_manager import (  # noqa: PLC0415
        load_tesla_tokens,
        load_tesla_config,
    )

    tesla_config = load_tesla_config()
    if tesla_config is None:
        return _json_response({  # type: ignore[return-value]
            "configured": False,
            "authenticated": False,
            "message": "Tesla Fleet API not configured in .env",
        })

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
