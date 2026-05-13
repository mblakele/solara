"""
Flask application providing energy usage metrics and TOU reporting.

Provides endpoints for real-time energy metrics and historical
Time-of-Use aggregation from Emporia VUE API. Includes load management
for solar self-consumption optimization.
"""

import asyncio
import atexit

import logging

import sys
import threading
import time

from datetime import datetime, timedelta, timezone
from typing import Any

import pytz
import requests
from flask import (
    Flask,
    Response,
    abort,
    make_response,
    render_template,
    request,
)
from flask.typing import ResponseReturnValue

from config import cfg as _cfg, get_timezone

from metrics import (
    EnergyCache,
    HourlyProjection,
    Metrics,
    TOUReporter,
    RetryableMetricsException,
)
from mockdata import MetricsMock
from util import CustomJSONProvider, is_debug

# Module-level lock for thread-safe first-call detection on _create_metrics.
_create_metrics_lock = threading.Lock()


def _create_metrics(logger: logging.Logger) -> dict[str, Any] | None:
    """Fetch metrics with incremental chart_start tracking via EnergyCache.

    On the first call, EnergyCache has no samples, so chart_start is set to
    3600 seconds ago (full hour of historical data). After that, chart_start
    advances to the most recent sample timestamp from the cache.

    Args:
        logger: Logger instance.

    Returns:
        Metrics dict from HourlyProjection, or None on failure.
    """
    now = datetime.now(pytz.timezone(_cfg.timezone))

    # First call: fetch entire previous hour.
    # Subsequent calls: fetch incremental data from the last sample timestamp.
    chart_start = (
        now - timedelta(seconds=3600)
        if _energy_cache.last_sample_at is None
        else _energy_cache.last_sample_at
    )

    hp = HourlyProjection(logger)
    hp.populate(chart_start)
    return hp.metrics


# global setup
from tesla_oauth import bp  # noqa: PLC0415

app = Flask(__name__)
app.logger.handlers.clear()
app.logger.propagate = True

# Register Tesla OAuth routes.
app.register_blueprint(bp)


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


def _trim_output_device(device: dict[str, Any]) -> dict[str, Any]:
    """Truncate per_second_data to 300 samples and move it to the end of the dict.

    Called on device dicts before they are sent to the template or JSON endpoint,
    ensuring the output is compact and debug-friendly.

    Args:
        device: A device dict from mock data or production to_dict().

    Returns:
        New dict with per_second_data truncated to last 300 and moved to end.
    """
    data = device.get("per_second_data", [])
    trimmed = list(data[-300:]) if len(data) > 300 else data
    # Build ordered dict with per_second_data last.
    ordered: dict[str, Any] = {}
    for k, v in device.items():
        if k == "per_second_data":
            continue
        ordered[k] = v
    ordered["per_second_data"] = trimmed
    return ordered


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
    is_mock = _cfg.is_mock_mode or force_mock
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
    is_mock = _cfg.is_mock_mode or force_mock
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

    Uses EnergyCache to avoid hammering the pyemvue API. In mock mode,
    falls back to MetricsMock for deterministic test data.
    """
    logger.debug("index")
    is_mock_error = _cfg.is_mock_error

    # Determine whether to use mock or real data
    is_mock = _cfg.is_mock_mode

    if is_mock:
        # Mock mode: use MetricsMock for deterministic test data
        instant_minute_str = request.args.get("instant_minute")
        instant_minute: int | None = None
        if instant_minute_str is not None:
            try:
                instant_minute = int(instant_minute_str)
            except (ValueError, TypeError):
                instant_minute = None
        model = _get_model(logger, is_mock_error, instant_minute=instant_minute)
        metrics_data = model.metrics
    else:
        # Real mode: use cached metrics to avoid hammering the API
        metrics_data, was_fresh = _energy_cache.get_or_fetch(
            lambda: _create_metrics(logger)
        )
        if was_fresh:
            logger.debug("Fetched fresh metrics for index endpoint")
        else:
            logger.debug("Serving cached metrics for index endpoint")

    # Recalculate lag from cached value + elapsed time since cache was stored.
    # The cached lag reflects data age at fetch time; since then data has
    # continued aging, so we add elapsed seconds to keep the display fresh.
    fetched_at = metrics_data.get("_fetched_at")
    if fetched_at is not None:
        elapsed = (datetime.now(timezone.utc) - fetched_at).total_seconds()
        for d in metrics_data.get("devices", []):
            cached_lag = d.get("lag", timedelta(0))
            d["lag"] = timedelta(seconds=cached_lag.total_seconds() + elapsed)

    # Truncate and reorder per_second_data in each device for compact output.
    metrics_data["devices"] = [_trim_output_device(d) for d in metrics_data.get("devices", [])]

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

# Shared cache to avoid hammering the pyemvue API.
# EnergyCache TTL (30s) undershoots the load management cycle interval.
_energy_cache = EnergyCache(ttl_seconds=30)

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
                    return _energy_cache.get_or_fetch(
                        lambda: _create_metrics(logger)
                    )[0]

                _load_manager = LoadManager(
                    metrics_fetch=metrics_fetch,
                    config_interval_secs=_cfg.load_manage_interval_secs,
                )
                logger.info("LoadManager initialized")
            except Exception as e:
                logger.warning("Failed to initialize LoadManager: %s", e)
                _load_manager_init_failed = True
        return _load_manager


def _load_management_loop() -> None:
    """Background thread that runs load management cycle with adaptive sleep."""
    interval_secs_config = _cfg.load_manage_interval_secs
    logger.info(
        "Load management background loop started (interval=%ds)", interval_secs_config
    )
    while True:
        try:
            lm = _get_load_manager()
            if lm is not None:
                result = lm.run_cycle()
                with _load_manager_lock:
                    _last_cycle_result = result
                logger.debug("Load management cycle result: %s", result)
            interval_secs = interval_secs_config
        except RetryableMetricsException as e:
            interval_secs = interval_secs_config
            logger.warning("Load management cycle retryable: %s", e)
        except Exception as e:
            interval_secs = interval_secs_config
            logger.error("Error in load management loop: %s", e)
        else:
            interval_secs = result.get("sleep_hint", interval_secs_config)
        logger.debug("Load management sleeping %d", interval_secs)
        time.sleep(interval_secs)


@app.route("/api/v1/load/manage", methods=["POST"])
def load_manage() -> Response:
    """Manually trigger a load management cycle.

    Accept optional ?force=true to bypass stale-data check (debug only).
    Returns JSON with status, current NBC prediction, pending effects, device states.
    Requires ``X-API-Key`` header matching ``LOAD_MANAGE_API_KEY`` env var when set.
    """
    required_key = _cfg.load_manage_api_key  # type: ignore[arg-type]
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
