"""Source-contract tests for the dashboard's OIDC read handling.

There is no JavaScript test runner in this project. These tests read
``static/app.js`` as text and pin the small auth contract the read gate
depends on: the header uses ``can_read``, partial fetches go through
``solaraFetch``, and a 401 or a signin-required SSE error sends the browser
to the signin route.
"""

from __future__ import annotations

import re
from pathlib import Path

APP_JS = Path(__file__).resolve().parents[1] / "static" / "app.js"


def _source() -> str:
    """Return the dashboard client source."""
    return APP_JS.read_text(encoding="utf-8")


def test_app_js_uses_can_read_for_auth_state() -> None:
    """The header distinguishes read access from write access."""
    source = _source()
    assert "me.can_read" in source
    assert "me.can_write" in source


def test_app_js_redirects_on_signin_required() -> None:
    """A signin-required state redirects once, preserving the current location."""
    source = _source()
    assert "function redirectToSignin" in source
    assert "signinRedirected" in source
    assert re.search(
        r"function redirectToSignin[\s\S]*?window\.location\.pathname"
        r"\s*\+\s*window\.location\.search",
        source,
    )


def test_app_js_partial_fetch_handles_401() -> None:
    """Fragment swaps use the wrapper that handles an expired session."""
    source = _source()
    assert re.search(r"function swapSection[\s\S]*?window\.solaraFetch\(", source)
    assert "resp.status === 401" in source


def test_app_js_sse_error_checks_signin_required() -> None:
    """SSE errors probe /api/me and redirect only when signin is required."""
    source = _source()
    assert re.search(r"source\.onerror\s*=\s*function[\s\S]*?api/me", source)
    assert re.search(r"source\.onerror\s*=\s*function[\s\S]*?signin_required", source)
