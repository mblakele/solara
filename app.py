"""
Flask application providing energy usage metrics and TOU reporting.

Provides endpoints for real-time energy metrics and historical
Time-of-Use aggregation from Emporia VUE API. Includes load management
for solar self-consumption optimization.
"""

import asyncio
import atexit
import html
import logging
import secrets
import sys
import threading
import time

from datetime import datetime
from typing import Any

import pytz
import requests
from flask import (
    Flask,
    Response,
    abort,
    make_response,
    redirect,
    render_template,
    request,
)
from flask.typing import ResponseReturnValue

from decouple import config
from metrics import (
    HourlyProjection,
    Metrics,
    MetricsCache,
    TOUReporter,
    RetryableMetricsException,
)
from mockdata import MetricsMock
from util import CustomJSONProvider, get_timezone, is_debug

# global setup
app = Flask(__name__)
app.logger.handlers.clear()
app.logger.propagate = True

def camelize(obj: object) -> object:
    """Convert snake_case keys to camelCase recursively."""
    if isinstance(obj, dict):
        new_dict = {}
        for k, v in obj.items():
            if isinstance(k, str) and "_" in k:
                parts = k.split("_")
                new_key = parts[0] + "".join(p.capitalize() for p in parts[1:])
            else:
                new_key = k
            new_dict[new_key] = camelize(v)
        return new_dict
    if isinstance(obj, list):
        return [camelize(i) for i in obj]
    return obj


# The template_folder and static_folder default to 'templates' and 'static'
# relative to the application path. Using the default root structure.


logger = app.logger
if __name__ != "__main__":
    gunicorn_logger = logging.getLogger("gunicorn.error")
    root_logger = logging.getLogger()
    root_logger.handlers = gunicorn_logger.handlers
    root_logger.setLevel(logging.DEBUG if is_debug() else logging.INFO)
else:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(
        "[%(asctime)s] [%(process)d] [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S %z",
    ))
    logging.basicConfig(handlers=[handler],
                        level=logging.DEBUG if is_debug() else logging.INFO)

# squelch internal log messages
for noisy in ("gunicorn.access", "boto3", "botocore", "urllib3", "requests"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

def astimezone_filter(dt: datetime, tz_str: str) -> datetime:
    """Convert datetime to specified timezone for Jinja2 template filter."""
    tz = pytz.timezone(tz_str)
    return dt.astimezone(tz)


def parse_date_to_utc(date_str: str) -> datetime:
    """Parse date string and convert to UTC timezone."""
    tz = pytz.timezone(get_timezone())
    if "T" in date_str:
        dt = datetime.fromisoformat(date_str)
    else:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
    if dt.tzinfo is None:
        dt = tz.localize(dt)

    return dt.astimezone(pytz.utc)


app.jinja_env.filters["astimezonestr"] = astimezone_filter


app.json = CustomJSONProvider(app)


def _get_model(
    logger: logging.Logger,
    is_mock_error: bool = False,
    force_mock: bool = False,
    instant_minute: int | None = None,
):
    """Select and return the appropriate data model (mock or real).

    Raises RetryableMetricsException when is_mock_error is True.
    Returns MetricsMock in mock mode, Metrics otherwise.

    Args:
        logger: Logger instance.
        is_mock_error: If True, raise RetryableMetricsException.
        force_mock: If True, use MetricsMock even if real credentials exist.
        instant_minute: For testing — sets the minute component of MetricsMock's
            simulated "now" time (0-59). Only used in mock mode.
    """
    if is_mock_error:
        raise RetryableMetricsException("mock")
    is_mock = config("VUE_USERNAME", None) is None or force_mock
    if is_mock:
        if instant_minute is not None:
            return MetricsMock(instant_minute=instant_minute)
        return MetricsMock()
    return Metrics(logger)


def _get_tou_model(start_date: datetime, end_date: datetime, force_mock: bool = False):
    """Return TOU buckets and NBC total based on configuration.

    Raises requests.exceptions.HTTPError or IOError from TOUReporter.
    Returns a dict with keys 'buckets' (TOU totals) and 'nbc' (total Wh
    across all 15-minute periods). In mock mode, returns realistic non-zero values.
    """
    is_mock = (
        config("VUE_USERNAME", None) is None
        or force_mock
        or config("MOCK", default="False", cast=bool)
    )
    if is_mock:
        mock = MetricsMock()
        return {"buckets": mock.tou_result, "nbc": mock.nbc_result}
    model = TOUReporter(start_date, end_date, logger)
    return {"buckets": model.tou_result, "nbc": model.nbc_result}


def _validate_dates(start_date_str: str | None, end_date_str: str | None):
    """Parse and validate date parameters.

    Returns (start_date, end_date) as UTC datetimes or aborts with 400.
    Defaults end_date to now if not provided.
    """
    if not start_date_str:
        return abort(400, "start_date is required")

    try:
        start_date = parse_date_to_utc(start_date_str)
    except (ValueError, TypeError):
        return abort(400, "Invalid start_date format")

    if end_date_str:
        try:
            end_date = parse_date_to_utc(end_date_str)
        except (ValueError, TypeError):
            return abort(400, "Invalid end_date format")
    else:
        end_date = pytz.utc.localize(datetime.now())

    date_diff = end_date - start_date
    if date_diff.days > 366:
        return abort(400, "Date range must be <= 366 days")

    return start_date, end_date


def _json_response(payload: Any) -> Response:
    """Create a JSON response with proper content type header."""
    resp = Response(app.json.dumps(payload))
    resp.headers["Content-Type"] = "application/json"
    return resp


@app.errorhandler(RetryableMetricsException)
def error_retryable(e: RetryableMetricsException) -> Response:
    """Handle retryable metrics exceptions with 5 second refresh."""
    resp = make_response(render_template("error_retryable.html", exception=e), 500)
    resp.headers["Refresh"] = "5"
    return resp


@app.route("/")
def index() -> ResponseReturnValue:
    """Main index endpoint serving HTML or JSON based on Accept header.

    Uses MetricsCache to avoid hammering the pyemvue API. In mock mode,
    falls back to MetricsMock for deterministic test data.
    """
    logger.debug("index")
    is_mock_error = config("MOCK_ERROR", default="False", cast=bool)
    force_mock = config("MOCK", default="False", cast=bool)

    # Determine whether to use mock or real data
    is_mock = config("VUE_USERNAME", None) is None or force_mock

    if is_mock:
        # Mock mode: use MetricsMock for deterministic test data
        instant_minute_str = request.args.get("instant_minute")
        instant_minute: int | None = None
        if instant_minute_str is not None:
            try:
                instant_minute = int(instant_minute_str)
            except (ValueError, TypeError):
                instant_minute = None
        model = _get_model(logger, is_mock_error, force_mock, instant_minute=instant_minute)
        metrics_data = model.metrics
    else:
        # Real mode: use cached metrics to avoid hammering the API
        metrics_data, was_fresh = _metrics_cache.get_or_fetch(
            lambda: HourlyProjection(logger).metrics
        )
        if was_fresh:
            logger.debug("Fetched fresh metrics for index endpoint")
        else:
            logger.debug("Serving cached metrics for index endpoint")

    # Gather load management state for display
    load_management = _build_load_management_payload()

    # check for default html first, to handle missing Accept header.
    if request.accept_mimetypes.accept_html:
        return render_template(
            "index.html",
            metrics=metrics_data,
            load_management=load_management,
        )

    if request.accept_mimetypes.accept_json:
        payload: dict = camelize(metrics_data)  # type: ignore[assignment]
        payload["loadManagement"] = camelize(load_management)
        return _json_response(payload)

    return abort(406)


@app.route("/health")
def health() -> Response:
    """Health check endpoint returning 'ok'."""
    logger.debug("health")
    resp = Response("ok")
    resp.headers["Content-Type"] = "text/plain"
    return resp


@app.route("/api/v1/tou")
def tou() -> ResponseReturnValue:
    """Time-of-Use API endpoint for energy consumption data."""
    logger.debug("tou")

    start_date_str = request.args.get("start_date")
    end_date_str = request.args.get("end_date")

    result = _validate_dates(start_date_str, end_date_str)
    if isinstance(result, Response):
        return result

    start_date, end_date = result

    try:
        tou_data = _get_tou_model(start_date, end_date)
    except (requests.exceptions.HTTPError, IOError) as e:
        error_msg = str(e)
        if isinstance(e, requests.exceptions.HTTPError) and e.response is not None:
            try:
                error_msg = f"{error_msg}: {e.response.text}"
            except (requests.exceptions.RequestException, AttributeError):
                pass
        logger.error("TOU error: %s", error_msg)
        return abort(500, f"Error fetching usage data: {error_msg}")

    buckets = tou_data["buckets"]
    nbc = tou_data["nbc"]

    if request.accept_mimetypes.accept_html:
        return render_template(
            "tou.html",
            start_date=start_date_str,
            end_date=end_date_str,
            buckets=buckets,
            nbc=nbc,
        )

    payload = {
        "start_date": start_date_str,
        "end_date": end_date_str,
        "buckets": buckets,
        "nbc": nbc,
    }
    return _json_response(payload)


# === Load Management State ===

# Shared caches to avoid hammering the pyemvue API.
# MetricsCache TTL (29s) undershoots the load management cycle interval.
# NBCCache TTL should be configured longer, as NBC predictions change slowly.
_metrics_cache = MetricsCache(ttl_seconds=29)

_load_manager = None
_load_manager_lock = threading.Lock()
_load_manager_init_failed = False
_last_cycle_result: dict = {}


def _build_load_management_payload() -> dict:
    """Build a load management state payload for the index endpoint.

    Returns a dict with enabled flag, device states, pending effects,
    and the last cycle result. Returns an empty dict if LoadManager
    is not initialized.
    """
    lm = _get_load_manager()
    if lm is None:
        return {}

    with _load_manager_lock:
        last_result = dict(_last_cycle_result)

    payload: dict = {
        "enabled": lm.enabled,
        "dry_run": lm.dry_run,
        "target_wh": lm.target_wh,
        "nbc_device": lm.nbc_device,
        "state": lm.state.to_dict(),
        "last_cycle_result": last_result,
    }

    return payload


def _get_load_manager():
    """Get or create the singleton LoadManager instance.

    If initialization has previously failed, returns None without retrying
    to avoid generating warnings on every call.
    """
    global _load_manager, _load_manager_init_failed
    with _load_manager_lock:
        if _load_manager is None and not _load_manager_init_failed:
            try:
                from load_manager import LoadManager

                def metrics_fetch():
                    return _metrics_cache.get_or_fetch(
                        lambda: HourlyProjection(logger).metrics
                    )[0]

                _load_manager = LoadManager(metrics_fetch=metrics_fetch)
                logger.info("LoadManager initialized")
            except Exception as e:
                logger.warning("Failed to initialize LoadManager: %s", e)
                _load_manager_init_failed = True
        return _load_manager


def _load_management_loop() -> None:
    """Background thread that runs load management cycle every 30 seconds."""
    interval_secs = config("LOAD_MANAGE_INTERVAL_SECS", default=30, cast=int)
    logger.info(
        "Load management background loop started (interval=%ds)", interval_secs
    )
    while True:
        try:
            lm = _get_load_manager()
            if lm is not None:
                result = lm.run_cycle()
                with _load_manager_lock:
                    _last_cycle_result = result
                logger.debug("Load management cycle result: %s", result)
        except RetryableMetricsException as e:
            logger.warning("Load management cycle retryable: %s", e)
        except Exception as e:
            logger.error("Error in load management loop: %s", e)
        time.sleep(interval_secs)


@app.route("/api/v1/load/manage", methods=["POST"])
def load_manage() -> Response:
    """Manually trigger a load management cycle.

    Accept optional ?force=true to bypass stale-data check (debug only).
    Returns JSON with status, current NBC prediction, pending effects, device states.
    Requires ``X-API-Key`` header matching ``LOAD_MANAGE_API_KEY`` env var when set.
    """
    required_key = config("LOAD_MANAGE_API_KEY", default="", cast=str)  # type: ignore[arg-type]
    if required_key:
        provided_key = request.headers.get("X-API-Key", "")
        if not provided_key or provided_key != required_key:
            return abort(401, "Unauthorized: valid X-API-Key header required")
    force = request.args.get("force", "false").lower() == "true"
    lm = _get_load_manager()
    if lm is None:
        return abort(503, "LoadManager not initialized")

    try:
        result = lm.run_cycle(force=force)
    except Exception as e:
        logger.error("Manual load management cycle failed: %s", e)
        return abort(500, f"Load management cycle failed: {e}")

    with _load_manager_lock:
        _last_cycle_result = result

    payload = camelize(result)
    return _json_response(payload)


@app.route("/api/v1/load/status")
def load_status() -> Response:
    """Read-only endpoint returning current load management state.

    Returns StateTracker state, last cycle result timestamp, enabled/disabled flag,
    and cache status.
    """
    lm = _get_load_manager()
    if lm is None:
        return abort(503, "LoadManager not initialized")

    with _load_manager_lock:
        last_result = dict(_last_cycle_result)

    payload = {
        "enabled": lm.enabled,
        "target_wh": lm.target_wh,
        "nbc_device": lm.nbc_device,
        "devices": {},
        "pending_effects": [],
        "last_cycle_result": last_result,
    }

    for name, device_state in lm.state.devices.items():
        payload["devices"][name] = {
            "desired_state": device_state.desired_state,
            "actual_state": device_state.actual_state,
            "current_amps": device_state.current_amps,
            "last_toggle": (
                device_state.last_toggle.isoformat()
                if device_state.last_toggle
                else None
            ),
        }

    for effect in lm.state.pending_effects:
        payload["pending_effects"].append(
            {
                "device_name": effect.device_name,
                "action": effect.action,
                "timestamp": effect.timestamp.isoformat(),
                "power_delta_wh": effect.power_delta_wh,
            }
        )

    return _json_response(camelize(payload))


# === Tesla OAuth Endpoints ===

# In-memory CSRF state store: maps random state token → expiry timestamp.
# Single-process; gunicorn must be run with a single worker for Tesla OAuth.
_oauth_states: dict[str, float] = {}


@app.route("/api/v1/tesla/auth/initiate")
def tesla_auth_initiate() -> ResponseReturnValue:
    """Initiate Tesla OAuth flow.

    Returns JSON with the authorization URL, or HTML with an auto-redirect
    when the client accepts text/html. The callback will be handled at /callback.
    """
    from load_manager import (
        RealTeslaController,
        load_tesla_config,
        load_tesla_tokens,
    )

    tesla_config = load_tesla_config()
    if tesla_config is None:
        return abort(503, "Tesla Fleet API not configured in .env")

    # Check if already authenticated
    tokens = load_tesla_tokens()

    if tokens and tokens.get("expires", 0) > time.time():
        valid_until = datetime.fromtimestamp(
            tokens["expires"], tz=pytz.UTC
        ).isoformat()
        if request.accept_mimetypes.accept_html:
            return (
                "<h1>Already Authenticated</h1>"
                f"<p>Tesla token valid until {html.escape(valid_until)}.</p>"
            )
        return _json_response({
            "authenticated": True,
            "message": f"Already authenticated. Token valid until: {valid_until}",
        })

    state_token = secrets.token_urlsafe(32)
    _oauth_states[state_token] = time.time() + 600  # expire in 10 minutes

    try:
        controller = RealTeslaController(tesla_config)
        login_url = controller.get_login_url(state=state_token)
    except Exception as e:
        logger.error("Failed to generate Tesla login URL: %s", e)
        _oauth_states.pop(state_token, None)
        return abort(500, f"Failed to generate login URL: {e}")

    if request.accept_mimetypes.accept_html:
        return redirect(login_url)

    return _json_response({
        "authenticated": False,
        "loginUrl": login_url,
        "message": "Open this URL in your browser to authorize Tesla access.",
    })


@app.route("/callback")
def tesla_auth_callback() -> ResponseReturnValue:
    """Handle OAuth callback from Tesla.

    Receives the authorization code, exchanges it for tokens, and persists them.
    Returns a success page on completion.
    """
    from load_manager import (
        RealTeslaController,
        save_tesla_tokens,
        load_tesla_config,
    )

    state = request.args.get("state", "")
    state_expiry = _oauth_states.pop(state, None)
    if not state or state_expiry is None or time.time() > state_expiry:
        return (
            "<h1>Tesla Auth Failed</h1>"
            "<p>Invalid or expired state parameter. "
            "Please restart the authentication flow.</p>",
            400,
        )

    code = request.args.get("code")
    if not code:
        return (
            "<h1>Tesla Auth Failed</h1><p>No authorization code received. "
            "Please try again.</p>",
            400,
        )

    tesla_config = load_tesla_config()
    if tesla_config is None:
        return (
            "<h1>Tesla Auth Failed</h1><p>Tesla Fleet API not configured.</p>",
            503,
        )

    async def _exchange():
        controller = RealTeslaController(tesla_config)
        await controller.exchange_code(code)
        # pylint: disable=protected-access
        # RealTeslaController internals needed to persist OAuth tokens.
        await controller._ensure_api()
        assert controller._api is not None
        save_tesla_tokens(
            refresh_token=controller._api.refresh_token,
            access_token=controller._api._access_token,
            expires=controller._api.expires,
        )

    try:
        asyncio.run(_exchange())
    except Exception as e:
        logger.error("Tesla OAuth token exchange failed: %s", e)
        return (
            f"<h1>Tesla Auth Failed</h1>"
            f"<p>Token exchange error: {html.escape(str(e))}</p>",
            500,
        )

    return (
        "<h1>Tesla Authentication Successful!</h1>"
        "<p>You can close this tab or navigate away.</p>"
        "<p>Your Tesla is now configured for load management.</p>"
    )


@app.route("/api/v1/tesla/status")
def tesla_status() -> Response:
    """Check Tesla authentication status.

    Returns whether a valid token exists and its expiration time.
    """
    from load_manager import load_tesla_tokens, load_tesla_config

    tesla_config = load_tesla_config()
    if tesla_config is None:
        return _json_response({
            "configured": False,
            "authenticated": False,
            "message": "Tesla Fleet API not configured in .env",
        })

    tokens = load_tesla_tokens()
    if tokens is None:
        return _json_response({
            "configured": True,
            "authenticated": False,
            "message": "Not authenticated. Visit /api/v1/tesla/auth/initiate to begin.",
        })

    expired = tokens.get("expires", 0) <= time.time()
    return _json_response({
        "configured": True,
        "authenticated": not expired,
        "tokenExpired": expired,
        "expiresAt": datetime.fromtimestamp(
            tokens["expires"], tz=pytz.UTC
        ).isoformat(),
    })


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--pair-plug":
        if len(sys.argv) < 5:
            print(
                "Usage: uv run python app.py --pair-plug <name> <address> <pin>"
            )
            sys.exit(1)

        from load_manager import pair_homekit_accessory

        plug_name = sys.argv[2]
        address = sys.argv[3]
        pin = sys.argv[4]

        print(f"Pairing HomeKit accessory '{plug_name}' at {address}...")
        success = pair_homekit_accessory(address, pin)
        if success:
            print("Pairing successful.")
            sys.exit(0)
        else:
            print("Pairing failed. Check logs for detail.")
            sys.exit(1)

    elif len(sys.argv) > 1 and sys.argv[1] == "--tesla-auth":
        from load_manager import tesla_auth_cli

        success = asyncio.run(tesla_auth_cli())
        if success:
            print("Tesla authentication successful.")
            sys.exit(0)
        else:
            print("Tesla authentication failed. Check logs for detail.")
            sys.exit(1)


_lm_thread_started = False


def _start_load_manager_thread():
    """Start the load management background thread (called once per process)."""
    global _lm_thread_started
    if _lm_thread_started:
        return
    _lm_thread_started = True
    lm_thread = threading.Thread(target=_load_management_loop, daemon=True)
    lm_thread.start()


def _shutdown_load_manager():
    """Clean up LoadManager resources on process exit."""
    with _load_manager_lock:
        if _load_manager is not None:
            try:
                _load_manager.close()
                logger.info("LoadManager shut down cleanly")
            except Exception as e:
                logger.warning("Error during LoadManager shutdown: %s", e)


atexit.register(_shutdown_load_manager)


# Start at module import time — runs once per gunicorn worker (forked after import)
# Skip during pytest to avoid background thread + unclosed aiohttp sessions
if "pytest" not in sys.modules:
    _start_load_manager_thread()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
