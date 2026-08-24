"""
Flask application providing energy usage metrics and TOU reporting.

Provides endpoints for real-time energy metrics and historical
Time-of-Use aggregation from Emporia VUE API. Includes load management
for solar self-consumption optimization.

The application is built by the :func:`create_app` factory. Importing
this module has no side effects beyond constructing the module-level
``app`` singleton: background threads (MQTT subscriber, load-management
loop) are only started by an explicit call to
:func:`start_background_services` (see wsgi.py and the ``__main__``
block), so tests and tooling can import the module safely.
"""

import asyncio
import atexit
from collections import deque
from dataclasses import dataclass, field

import logging
import logging.handlers

import sys
import signal
import threading

from datetime import datetime, timedelta, timezone
from typing import Any

import pytz
import requests
from flask import (
    Flask,
    Response,
    abort,
    current_app,
    make_response,
    render_template,
    request,
)
from flask.typing import ResponseReturnValue

from config import Config, _config, get_timezone
from clock import Clock, RealClock
from constants import STALE_DATA_THRESHOLD_SECS

from energy_cache import EnergyCache
import logfmt
from metrics import (
    create_metrics,
    Metrics,
    TOUReporter,
    TOUResult,
    RetryableMetricsException,
)
from mockdata import MetricsMock
import mqtt_telemetry
from load_models import CycleResult
from sse_event import SSEBroadcaster, event_stream
from util import CustomJSONProvider, is_debug

from tesla_oauth import bp


@dataclass
class _AppState:
    """Mutable runtime state shared across views and background threads.

    Holds the module-level singletons that used to be plain globals so
    that ``create_app()`` can construct the app without side effects and
    tests can reset state by assigning fields on the instance.
    """

    energy_cache: EnergyCache
    sse_broadcaster: SSEBroadcaster
    load_manager: Any = None
    load_manager_lock: threading.Lock = field(default_factory=threading.Lock)
    load_manager_init_failed: bool = False
    last_cycle_result: CycleResult | None = None
    recent_cycles: deque[dict[str, Any]] = field(
        default_factory=lambda: deque(maxlen=10)
    )
    telegram_sender: Any = None
    consecutive_error_count: int = 0
    last_error_type: str | None = None
    lm_thread_started: bool = False
    # Init-retry state (plan 2.6): failures back off instead of latching.
    load_manager_init_attempts: int = 0
    load_manager_next_init_retry_at: datetime | None = None
    # Liveness/health signals (plan 1.5).
    lm_heartbeat_at: datetime | None = None
    mqtt_subscriber_started: bool = False
    background_services_started_at: datetime | None = None
    # Watchdog input (plan 2.7): last completed loop iteration.
    lm_last_cycle_finished_at: datetime | None = None


# Application-level configuration injected into all consumers.
_config = Config()

# Shared runtime state (cache, broadcaster, load-manager singleton, ...).
_state = _AppState(
    energy_cache=EnergyCache(ttl_seconds=60),
    sse_broadcaster=SSEBroadcaster(),
)

# Cooperative shutdown signal (single-interrupt exit): background loops wait
# on this event instead of a bare sleep so a stop request ends them
# immediately. See request_shutdown().
_stop_event = threading.Event()
# Guard so signal-hook installation happens once (see
# install_shutdown_signal_hooks()).
_shutdown_hooks_installed = False


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
        device: A device dict from mock data or production.

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


def _enrich_metrics_for_sse(metrics_data: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    """Apply lag recalculation, sample merging, and output trimming.

    Mirrors the runtime adjustments originally inlined in index() so
    SSE clients see current lag and accumulated per-second samples.

    Args:
        metrics_data: The metrics dict from a fetch or cache. Device entries
            are shallow-copied before enrichment, so the caller's dict is
            never mutated (get_or_fetch may return the same dict stored as
            full_metrics_dict — mutating it would inflate cached lag).
        now: Current time for lag calculation. Defaults to datetime.now(timezone.utc).

    Returns:
        The enriched metrics dict: the same top-level object, with device
        entries replaced by enriched copies.
    """
    if metrics_data is None:
        metrics_data = {"devices": [], "api_response": {}, "instant": now}
    if now is None:
        now = datetime.now(timezone.utc)
    # Shallow-copy device entries before mutating them: the source dict may
    # be the cached full_metrics_dict, and in-place lag updates there would
    # accumulate elapsed time on every enrich pass.
    metrics_data["devices"] = [dict(d) for d in metrics_data.get("devices", [])]
    fetched_at = metrics_data.get("_fetched_at")
    if fetched_at is not None:
        elapsed = (now - fetched_at).total_seconds()
        for d in metrics_data.get("devices", []):
            cached_lag = d.get("lag", timedelta(0))
            d["lag"] = timedelta(seconds=cached_lag.total_seconds() + elapsed)
    samples = _state.energy_cache.samples
    if samples:
        accumulated = list(samples)
        devices = metrics_data.get("devices", [])
        if len(devices) == 1:
            devices[0]["per_second_data"] = accumulated
    metrics_data["devices"] = [_trim_output_device(d) for d in metrics_data.get("devices", [])]
    return metrics_data


# The template_folder and static_folder default to 'templates' and 'static'
# relative to the application path. Using the default root structure.


def _setup_file_logging(config: Config) -> logging.Handler | None:
    """Create a RotatingFileHandler if LOG_FILE is configured.

    Returns the handler so callers can attach it to additional loggers
    (e.g. gunicorn.error), or None if file logging is disabled.
    """
    log_file = config.log_file
    if not log_file:
        return None
    handler = logging.handlers.RotatingFileHandler(
        log_file,
        maxBytes=config.log_max_bytes,
        backupCount=config.log_backup_count,
    )
    json_mode = str(config.get("LOG_FORMAT", "text")).lower() == "json"
    handler.setFormatter(logfmt.create_formatter(
        json_mode,
        fmt="[%(asctime)s] [%(process)d] [%(levelname)s] %(name)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S %z",
    ))
    return handler


# squelch internal log messages
for noisy in (
        "asyncio", "boto3", "botocore", "gunicorn.access",
        "urllib3", "requests"):
    logging.getLogger(noisy).setLevel(logging.WARNING)


def astimezone_filter(dt: datetime, tz_str: str) -> datetime:
    """Convert datetime to specified timezone for Jinja2 template filter."""
    tz = pytz.timezone(tz_str)
    return dt.astimezone(tz)


def parse_date_to_utc(date_str: str) -> datetime:
    """Parse date string and convert to UTC timezone.

    Raises:
        ValueError: If the string is malformed, or names a local time that
            is ambiguous (DST fall-back overlap) or nonexistent (DST
            spring-forward gap) in the configured timezone. Failing
            explicitly beats silently picking the wrong UTC instant.
    """
    tz = pytz.timezone(get_timezone())
    if "T" in date_str:
        dt = datetime.fromisoformat(date_str)
    else:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
    if dt.tzinfo is None:
        try:
            # is_dst=None makes ambiguous/nonexistent local times raise
            # instead of silently resolving to an arbitrary offset.
            dt = tz.localize(dt, is_dst=None)
        except pytz.exceptions.InvalidTimeError as exc:
            raise ValueError(
                f"Ambiguous or nonexistent local time: {date_str!r}"
            ) from exc

    return dt.astimezone(pytz.utc)


def _get_model(
    logger: logging.Logger,
    instant: datetime,
    is_mock_error: bool = False,
    force_mock: bool = False,
    instant_minute: int | None = None,
):
    """Select and return the appropriate data model (mock or real).

    Raises RetryableMetricsException when is_mock_error is True.
    Returns MetricsMock in mock mode, Metrics otherwise.

    Args:
        logger: Logger instance.
        instant: Current datetime for the Metrics model.
        is_mock_error: If True, raise RetryableMetricsException.
        force_mock: If True, use MetricsMock even if real credentials exist.
        instant_minute: For testing — sets the minute component of MetricsMock's
            simulated "now" time (0-59). Only used in mock mode.
    """
    if is_mock_error:
        raise RetryableMetricsException("mock")
    is_mock = _config.is_mock_mode or force_mock
    if is_mock:
        if instant_minute is not None:
            return MetricsMock(instant_minute=instant_minute)
        return MetricsMock()
    return Metrics(instant, logger, config=_config)


def _get_tou_model(start_date: datetime, end_date: datetime, force_mock: bool = False) -> TOUResult:
    """Return TOU buckets and NBC total based on configuration.

    Raises requests.exceptions.HTTPError or IOError from TOUReporter.
    Returns a TOUResult with buckets (TOU totals) and nbc (total Wh
    across all 15-minute periods). In mock mode, returns realistic non-zero values.
    """
    is_mock = _config.is_mock_mode or force_mock
    if is_mock:
        mock = MetricsMock()
        return TOUResult(buckets=mock.tou_result, nbc=mock.nbc_result)
    model = TOUReporter(start_date, end_date, logger, config=_config)
    assert model.tou_result is not None
    assert model.nbc_result is not None
    return TOUResult(buckets=model.tou_result, nbc=model.nbc_result)


def _validate_dates(
    start_date_str: str | None,
    end_date_str: str | None,
    clock: Clock | None = None,
):
    """Parse and validate date parameters.

    Returns (start_date, end_date) as UTC datetimes or aborts with 400.
    Defaults end_date to the injected clock's current time when not
    provided (RealClock in production — always a correctly localized,
    timezone-aware instant; FakeClock in tests).

    Args:
        start_date_str: Start date string, or None to abort with 400.
        end_date_str: End date string; defaults to now via *clock*.
        clock: Time source for the default end date. Defaults to RealClock.
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
        end_date = (clock or RealClock()).now()

    date_diff = end_date - start_date
    if date_diff.days > 366:
        return abort(400, "Date range must be <= 366 days")

    return start_date, end_date


def _json_response(payload: Any) -> Response:
    """Create a JSON response with proper content type header."""
    resp = Response(current_app.json.dumps(payload))
    resp.headers["Content-Type"] = "application/json"
    return resp


def error_retryable(e: RetryableMetricsException) -> Response:
    """Handle retryable metrics exceptions with 5 second refresh."""
    resp = make_response(render_template("error_retryable.html", exception=e), 500)
    resp.headers["Refresh"] = "5"
    return resp


def index() -> ResponseReturnValue:
    """Main index endpoint serving HTML or JSON based on Accept header.

    In mock mode, falls back to MetricsMock for deterministic test data.
    """
    logger.debug("index")
    is_mock_error = _config.is_mock_error

    if is_mock_error:
        raise RetryableMetricsException("mock error")

    # Determine whether to use mock or real data
    is_mock = _config.is_mock_mode

    now = datetime.now(timezone.utc)

    if is_mock:
        # Mock mode: use MetricsMock for deterministic test data
        instant_minute_str = request.args.get("instant_minute")
        instant_minute: int | None = None
        if instant_minute_str is not None:
            try:
                instant_minute = int(instant_minute_str)
            except (ValueError, TypeError):
                instant_minute = None
        model = _get_model(logger, now, is_mock_error, instant_minute=instant_minute)
        metrics_data = model.metrics
    else:
        # Real mode: use cached metrics to avoid hammering the API
        metrics_data, was_fresh = _state.energy_cache.get_or_fetch(
            lambda: create_metrics(_state.energy_cache, datetime.now(pytz.timezone(_config.timezone)), logger),
            now
        )
        if was_fresh:
            logger.debug("Fetched fresh metrics for index endpoint")
        else:
            logger.debug("Serving cached metrics for index endpoint")

    # Enrich metrics for output: recalculate lag, merge samples, trim output.
    metrics_data = _enrich_metrics_for_sse(metrics_data, now=now)

    # Gather load management state for display
    load_management = _build_load_management_payload()

    # check for default html first, to handle missing Accept header.
    if request.accept_mimetypes.accept_html:
        refresh_secs: int | None = None
        if not metrics_data.get("devices"):
            # First-boot API outage: the 500 retry page is dead for
            # real-data paths (RetryableMetricsException is downgraded to a
            # warning + stale-cache serve in _run_fetch_with_timeout), so an
            # empty dashboard renders with a 200. Auto-refresh it so it
            # self-heals when data arrives — no manual reload needed.
            refresh_secs = 5
            logger.warning(
                "index: serving empty dashboard (no devices); auto-refreshing in %ds",
                refresh_secs,
            )
        return render_template(
            "index.html",
            metrics=metrics_data,
            load_management=load_management,
            refresh_secs=refresh_secs,
        )

    if request.accept_mimetypes.accept_json:
        payload: dict = camelize(metrics_data)  # type: ignore[assignment]
        payload["loadManagement"] = camelize(load_management)
        return _json_response(payload)

    return abort(406)


# Health-check tuning (plan 1.5): gates are deliberately conservative to
# avoid restart storms during boot or when load management is disabled.
_HEALTH_BOOT_GRACE_SECS = 300.0
_HEALTH_MQTT_DARK_SECS = 600.0
_HEALTH_MQTT_DISCONNECT_STALE_SECS = 60.0
_HEALTH_ERROR_THRESHOLD = 3
_HEALTH_LM_MIN_STALE_SECS = 120.0

# LoadManager init-retry tuning (plan 2.6).
_LM_INIT_RETRY_BASE_SECS = 30.0
_LM_INIT_RETRY_MAX_SECS = 600.0


def _lm_init_backoff_secs(attempts: int) -> float:
    """Exponential backoff for LoadManager init retries, capped.

    Args:
        attempts: 1-based count of failed init attempts.

    Returns:
        Seconds to wait before the next attempt.
    """
    return min(
        _LM_INIT_RETRY_BASE_SECS * (2 ** max(0, attempts - 1)),
        _LM_INIT_RETRY_MAX_SECS,
    )


def _compute_loop_sleep(result: Any, interval_secs: float) -> float:
    """Compute the LM loop's sleep duration defensively.

    A negative sleep hint or an exception from
    ``EnergyCache.sleep_interval_adjust`` must never kill the background
    thread (R10): clamp to >= 0 and fall back to the configured interval.

    Args:
        result: The cycle result (or None) from ``run_cycle``.
        interval_secs: Configured polling interval fallback.

    Returns:
        Non-negative seconds to sleep.
    """
    try:
        if result is not None and result.status == "disabled":
            raw = float(interval_secs)
        else:
            raw = _state.energy_cache.sleep_interval_adjust(
                interval_secs,
                datetime.now(pytz.timezone(_config.timezone)),
            )
    except Exception:  # pylint: disable=broad-exception-caught
        logger.warning(
            "sleep_interval_adjust failed; using configured interval",
            exc_info=True,
        )
        raw = float(interval_secs)
    return max(0.0, raw)


_stall_critical_last_at: datetime | None = None


def _check_stall_watchdog(now: datetime) -> None:
    """CRITICAL-log when enabled load management stops completing cycles.

    Rate-limited to one entry per hour so a prolonged stall stays visible
    without flooding logs.

    Called by ``_load_management_loop`` at the END of each iteration,
    BEFORE refreshing ``lm_last_cycle_finished_at`` — so it measures the
    duration of the iteration that just finished. Residual limitation: a
    thread that hangs mid-cycle never reaches this check at all; that
    case is covered by the /health heartbeat staleness instead.
    """
    global _stall_critical_last_at
    if _config.load_manage_enabled is False:
        return
    finished_at = _state.lm_last_cycle_finished_at
    if finished_at is None:
        return
    threshold = max(
        300.0, 10.0 * float(_config.load_manage_interval_secs)
    )
    stalled_for = (now - finished_at).total_seconds()
    if stalled_for <= threshold:
        return
    if (
        _stall_critical_last_at is not None
        and (now - _stall_critical_last_at).total_seconds() < 3600
    ):
        return
    _stall_critical_last_at = now
    logger.critical(
        "Load management stalled: no completed cycle for %.0fs "
        "(threshold %.0fs)",
        stalled_for,
        threshold,
    )


def _build_health_payload(now: datetime) -> dict[str, Any]:
    """Compute component health for the /health endpoint.

    Args:
        now: Current time (injected so tests can pin it).

    Returns:
        Dict with overall ``status`` ("ok" or "degraded") plus a
        ``components`` map: load_manager_thread, energy_cache,
        mqtt_telemetry, errors.
    """

    def _iso(value: datetime | None) -> str | None:
        return value.isoformat() if value is not None else None

    # ── Load-management thread liveness ──────────────────────────────
    lm_enabled = _config.load_manage_enabled is not False
    heartbeat = _state.lm_heartbeat_at
    lm_state = "disabled"
    lm_age: float | None = None
    if lm_enabled:
        if heartbeat is None:
            lm_state = "never_run"
        else:
            lm_age = (now - heartbeat).total_seconds()
            stale_bound = max(
                _HEALTH_LM_MIN_STALE_SECS,
                3.0 * float(_config.load_manage_interval_secs),
            )
            lm_state = "alive" if lm_age <= stale_bound else "stale"

    # ── Emporia sample-cache freshness ────────────────────────────────
    cache_data = _state.energy_cache.data
    cache_state = "empty"
    cache_age: float | None = None
    if (
        cache_data is not None
        and cache_data.samples
        and cache_data.data_start is not None
    ):
        # Per the contiguity axiom sample i occurs at data_start + i·s, so
        # the newest of N samples sits at data_start + (N-1)·s. Prefer the
        # maintained last_sample_at; the arithmetic is only a fallback.
        newest_sample = cache_data.last_sample_at or (
            cache_data.data_start
            + timedelta(seconds=len(cache_data.samples) - 1)
        )
        cache_age = (now - newest_sample).total_seconds()
        cache_state = (
            "fresh"
            if cache_age <= 2 * STALE_DATA_THRESHOLD_SECS
            else "stale"
        )
    started_at = _state.background_services_started_at
    booted_long_ago = (
        started_at is not None
        and (now - started_at).total_seconds() > _HEALTH_BOOT_GRACE_SECS
    )
    cache_gated = lm_enabled and (
        cache_state == "stale"
        or (cache_state == "empty" and booted_long_ago)
    )

    # ── MQTT telemetry feed ───────────────────────────────────────────
    freshness = mqtt_telemetry.get_field_freshness()
    mqtt_last_age = (
        (now - max(freshness.values())).total_seconds()
        if freshness
        else None
    )
    mqtt_connected: bool | None = None
    if not _state.mqtt_subscriber_started:
        mqtt_state = "not_started"
        mqtt_gated = False
    else:
        mqtt_connected = mqtt_telemetry.is_connected()
        if mqtt_last_age is None:
            mqtt_state = "waiting"
            mqtt_gated = booted_long_ago
        elif mqtt_last_age > _HEALTH_MQTT_DARK_SECS:
            # Most severe: no usable data for >10 min, whatever the cause.
            mqtt_state = "dark"
            mqtt_gated = True
        elif (
            not mqtt_connected
            and mqtt_last_age > _HEALTH_MQTT_DISCONNECT_STALE_SECS
        ):
            # Feed went dark recently enough that old cached messages
            # still look "fresh" — connection state closes that gap.
            mqtt_state = "disconnected"
            mqtt_gated = True
        else:
            mqtt_state = "receiving"
            mqtt_gated = False

    # ── Sustained cycle errors ────────────────────────────────────────
    errors_gated = (
        _state.consecutive_error_count >= _HEALTH_ERROR_THRESHOLD
    )

    degraded = (
        lm_state == "stale" or cache_gated or mqtt_gated or errors_gated
    )
    return {
        "status": "degraded" if degraded else "ok",
        "components": {
            "load_manager_thread": {
                "state": lm_state,
                "last_heartbeat_at": _iso(heartbeat),
                "age_secs": lm_age,
            },
            "energy_cache": {
                "state": cache_state,
                "age_secs": cache_age,
            },
            "mqtt_telemetry": {
                "state": mqtt_state,
                "connected": mqtt_connected,
                "has_telemetry": bool(freshness),
                "last_update_age_secs": mqtt_last_age,
            },
            "errors": {
                "consecutive_error_count": _state.consecutive_error_count,
                "last_error_type": _state.last_error_type,
            },
        },
    }


def health() -> Response:
    """Component health endpoint (always HTTP 200).

    Deploy tooling inspects the JSON ``status``/``components`` fields
    rather than the HTTP code, so a degraded instance can be observed
    and alerted on, not just blindly restarted.
    """
    payload = _build_health_payload(datetime.now(timezone.utc))
    return _json_response(camelize(payload))


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

    buckets = tou_data.buckets
    nbc = tou_data.nbc

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
        "buckets": buckets.to_dict(),
        "nbc": nbc,
    }
    return _json_response(payload)


# === Load Management State ===


def _cycle_result_to_dict(result: CycleResult | dict | None) -> dict:
    """Convert a CycleResult to a plain dict for JSON serialization.

    Accepts both CycleResult objects (calling .to_dict()) and plain dicts
    (returned directly) for compatibility with existing tests.

    Args:
        result: The CycleResult or dict to convert.

    Returns:
        A plain dict representation suitable for JSON serialization
        and template rendering.
    """
    if result is None:
        return {}
    if isinstance(result, dict):
        return result
    return result.to_dict()


def _build_load_management_payload(lm: Any = None) -> dict:
    """Build a load management state payload for the index endpoint.

    Args:
        lm: Optional LoadManager instance.  When provided, the caller
            already holds ``_state.load_manager_lock`` (e.g. the background
            loop) and the payload is built from this instance without
            re-acquiring the lock or calling ``_get_load_manager()``.
            When omitted, the load manager is resolved under the lock;
            an empty dict is returned if load management is disabled or
            the manager is unavailable.

    Returns:
        Dict with enabled flag, device states, pending effects, and the
        last cycle result; an empty dict when unavailable.
    """
    if lm is None:
        if _config.load_manage_enabled is False:
            return {}
        lm = _get_load_manager()
        if lm is None:
            return {}
        with _state.load_manager_lock:
            last_result = _cycle_result_to_dict(_state.last_cycle_result) if _state.last_cycle_result else {}
    else:
        last_result = _cycle_result_to_dict(_state.last_cycle_result) if _state.last_cycle_result else {}

    payload: dict = {
        "enabled": lm.enabled,
        "dry_run": lm.dry_run,
        "target_wh": lm.target_wh,
        "nbc_device": lm.nbc_device,
        "state": lm.state.to_dict(),
        "last_cycle_result": last_result,
        "sleep_hint": last_result.get("sleep_hint", lm.config_interval_secs),
        "sleep_hint_at": last_result.get("sleep_hint_at"),
    }

    return payload


def _get_load_manager():
    """Get or create the singleton LoadManager instance.

    Initialization failures are not permanent (plan 2.6): the next call
    after the backoff window elapses retries construction, so a transient
    bad devices.json heals without a process restart.
    """
    with _state.load_manager_lock:
        if _state.load_manager is None:
            now_ = datetime.now(timezone.utc)
            if _state.load_manager_init_failed:
                next_at = _state.load_manager_next_init_retry_at
                if next_at is not None and now_ < next_at:
                    return None
            try:
                from load_manager import LoadManager, LoadManagerConfig

                def metrics_fetch():
                    now = datetime.now(timezone.utc)
                    return _state.energy_cache.get_or_fetch(
                        lambda: create_metrics(_state.energy_cache, datetime.now(pytz.timezone(_config.timezone)), logger),
                        now,
                        force=True
                    )[0]

                # Wire up Telegram notifications if configured (env vars or
                # devices.json telegram section).
                from telegram import TelegramSender

                telegram_sender = TelegramSender.from_config()
                if telegram_sender is not None:
                    logger.info(
                        "Telegram notifications enabled for chat %s",
                        telegram_sender.config.chat_id,
                    )
                else:
                    logger.info("Telegram notifications disabled (no config)")

                _state.telegram_sender = telegram_sender

                _state.load_manager = LoadManager(
                    LoadManagerConfig(
                        config=_config,
                        metrics_fetch=metrics_fetch,
                        config_interval_secs=_config.load_manage_interval_secs,
                        telegram_sender=telegram_sender,
                        energy_cache=_state.energy_cache,
                    ),
                )
                logger.info("LoadManager initialized")
                _state.load_manager_init_failed = False
                _state.load_manager_init_attempts = 0
                _state.load_manager_next_init_retry_at = None

            except Exception as e:
                _state.load_manager_init_attempts += 1
                delay = _lm_init_backoff_secs(
                    _state.load_manager_init_attempts
                )
                _state.load_manager_next_init_retry_at = now_ + timedelta(
                    seconds=delay
                )
                _state.load_manager_init_failed = True
                logger.warning(
                    "Failed to initialize LoadManager "
                    "(attempt %d, retry in %.0fs): %s",
                    _state.load_manager_init_attempts,
                    delay,
                    e,
                    exc_info=True,
                )
        return _state.load_manager


def _send_error_alert(exc: Exception) -> None:
    """Send a telegram error alert for background loop errors.

    Args:
        exc: The exception that triggered the alert.
    """
    from telegram import build_error_notification

    if _state.telegram_sender is None or not _state.telegram_sender.is_configured:
        return
    event = build_error_notification(f"{type(exc).__name__}: {exc}")
    try:
        _state.telegram_sender.send_notification_sync(event)
    except Exception:  # pylint: disable=broad-exception-caught
        logger.warning("Failed to send error alert", exc_info=True)


def _load_management_loop() -> None:
    """Background thread that runs load management cycle with adaptive sleep."""
    interval_secs_config = _config.load_manage_interval_secs
    logger.info(
        "Load management background loop started: dry-run=%s, mock=%s, interval=%d",
        _config.dry_run, _config.is_mock_mode, interval_secs_config
    )
    while not _stop_event.is_set():
        _state.lm_heartbeat_at = datetime.now(timezone.utc)
        result = None
        try:
            lm = _get_load_manager()
            if lm is not None:
                result = lm.run_cycle()
                lm._send_pending_notifications_sync()  # flush Telegram sends outside lock
                with _state.load_manager_lock:
                    _state.last_cycle_result = result
                    _state.recent_cycles.append({
                        "cycle_id": result.cycle_id,
                        "status": result.status,
                        "reason": result.diagnostics.reason if result.diagnostics else None,
                        "actions_count": len(result.actions),
                        "sleep_hint": result.sleep_hint,
                        "gap_wh": result.gap_wh,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    })
                    lm_payload = _build_load_management_payload(lm)
                logger.debug("Load management cycle result: %s", result)
                if (
                    result.status == "no_incomplete_qh"
                    and _state.energy_cache.data is None
                ):
                    logger.warning(
                        "Load management: no data available (possible network issue)"
                    )
                _state.sse_broadcaster.publish("load_cycle", camelize(lm_payload))
                full_metrics_dict = _state.energy_cache.full_metrics_dict
                if full_metrics_dict is not None:
                    _state.sse_broadcaster.publish(
                        "metrics_update",
                        camelize(_enrich_metrics_for_sse(dict(full_metrics_dict))),
                    )
            interval_secs = interval_secs_config
        except RetryableMetricsException as e:
            interval_secs = interval_secs_config
            logger.warning("Load management cycle retryable: %s", e)
        except Exception as e:
            interval_secs = interval_secs_config
            _state.consecutive_error_count += 1
            _state.last_error_type = type(e).__name__
            logger.error(
                "Error in load management loop: %s", e, exc_info=True
            )
            _state.energy_cache.invalidate()
            if _state.consecutive_error_count == 1 or _state.consecutive_error_count % 10 == 0:
                _send_error_alert(e)
        else:
            if result is not None:
                interval_secs = result.sleep_hint
            _state.consecutive_error_count = 0
            _state.last_error_type = None

        # Check BEFORE refreshing: the watchdog must measure how long the
        # iteration that just completed took. Refreshing first (the original
        # ordering) made stalled_for ≈ 0 every cycle and the CRITICAL branch
        # unreachable — see review finding #1.
        now = datetime.now(timezone.utc)
        _check_stall_watchdog(now)
        _state.lm_last_cycle_finished_at = now
        interval_secs_adjusted = _compute_loop_sleep(result, interval_secs)
        logger.debug("Load management sleeping %.1f", interval_secs_adjusted)
        # Event-based sleep: request_shutdown() ends the wait (and this
        # loop) immediately instead of after the full interval.
        if _stop_event.wait(interval_secs_adjusted):
            break

def load_status() -> Response:
    """Read-only endpoint returning current load management state.

    Returns StateTracker state, last cycle result timestamp, enabled/disabled flag,
    and cache status.
    """
    lm = _get_load_manager()
    if lm is None:
        return abort(503, "LoadManager not initialized")

    with _state.load_manager_lock:
        last_result = _cycle_result_to_dict(_state.last_cycle_result) if _state.last_cycle_result else {}

    cache_data = _state.energy_cache.data
    if cache_data is not None:
        cache_payload = {
            "data_start": (
                cache_data.data_start.isoformat()
                if cache_data.data_start
                else None
            ),
            "last_fetch_at": (
                cache_data.last_fetch_at.isoformat()
                if cache_data.last_fetch_at
                else None
            ),
            "sample_count": cache_data.sample_count,
        }
    else:
        cache_payload = None

    mqtt_updates = mqtt_telemetry.get_field_freshness()
    last_update_at = max(mqtt_updates.values()) if mqtt_updates else None
    mqtt_age = (
        (datetime.now(timezone.utc) - last_update_at).total_seconds()
        if last_update_at is not None
        else None
    )

    payload = {
        "enabled": lm.enabled,
        "target_wh": lm.target_wh,
        "nbc_device": lm.nbc_device,
        "devices": {},
        "pending_effects": [],
        "last_cycle_result": last_result,
        "recent_cycles": list(_state.recent_cycles),
        "consecutive_error_count": _state.consecutive_error_count,
        "last_error_type": _state.last_error_type,
        "sse_subscriber_count": _state.sse_broadcaster.subscriber_count(),
        "cache": cache_payload,
        "mqtt": {
            "has_telemetry": mqtt_telemetry.has_telemetry(),
            "connected": mqtt_telemetry.is_connected(),
            "last_update_age_secs": mqtt_age,
        },
    }

    for name, device_state in lm.state.snapshot_devices().items():
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

    for effect in lm.state.snapshot_effects():
        payload["pending_effects"].append(
            {
                "device_name": effect.device_name,
                "action": effect.action,
                "timestamp": effect.timestamp.isoformat(),
            }
        )
    return _json_response(camelize(payload))


def stream_status():
    """SSE endpoint streaming load management state and metrics updates.

    On connect, emits an initial_load_state event with the current load
    management payload, and an initial_metrics event (if cached metrics
    are available). Then subscribes to the SSE broadcaster for ongoing
    load_cycle and metrics_update events as they occur.
    """
    initial: list[tuple[str, object]] = [
        ("initial_load_state", camelize(_build_load_management_payload())),
    ]
    full_metrics_dict = _state.energy_cache.full_metrics_dict
    if full_metrics_dict is not None:
        initial.append(
            ("initial_metrics", camelize(_enrich_metrics_for_sse(dict(full_metrics_dict))))
        )
    return Response(
        event_stream(_state.sse_broadcaster, initial_events=initial, dumper=current_app.json.dumps),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        }
    )


def _start_mqtt_subscriber() -> None:
    """Start the MQTT subscriber thread for Tesla fleet-telemetry events."""
    from mqtt_telemetry import start_mqtt_subscriber as _start
    _start(_config)
    _state.mqtt_subscriber_started = True
    logger.info("MQTT subscriber started")


def _start_load_manager_thread():
    """Start the load management background thread (called once per process)."""
    if _state.lm_thread_started:
        return
    _state.lm_thread_started = True
    lm_thread = threading.Thread(target=_load_management_loop, daemon=True)
    lm_thread.start()


def _shutdown_load_manager():
    """Clean up LoadManager resources on process exit.

    Nulls the manager before closing so re-entrant calls (atexit after
    request_shutdown) are natural no-ops.
    """
    with _state.load_manager_lock:
        lm = _state.load_manager
        if lm is not None:
            _state.load_manager = None
            try:
                lm.close()
                logger.info("LoadManager shut down cleanly")
            except Exception as e:
                logger.warning("Error during LoadManager shutdown: %s", e)


def request_shutdown(reason: str) -> None:
    """Request cooperative shutdown of background services.

    Idempotent and safe to call from signal handlers, gunicorn worker hooks,
    and atexit: sets the stop event every background loop waits on, ends all
    SSE streams so no worker-pool thread blocks interpreter finalization,
    stops the MQTT subscriber, and closes the LoadManager exactly once.

    Args:
        reason: Short tag identifying the shutdown trigger (for logs).
    """
    if _stop_event.is_set():
        return
    logger.info("Shutdown requested (%s)", reason)
    _stop_event.set()
    _state.sse_broadcaster.close_all()
    if _state.mqtt_subscriber_started:
        from mqtt_telemetry import stop_mqtt_subscriber  # avoid import cycle

        stop_mqtt_subscriber()
    _shutdown_load_manager()


def install_shutdown_signal_hooks() -> None:
    """Chain cooperative-shutdown handlers over installed signal handlers.

    For each of SIGINT/SIGQUIT/SIGTERM whose current disposition is a
    callable handler (gunicorn's, Python's default, ...), installs a
    wrapper that calls :func:`request_shutdown` first and then delegates,
    preserving the host's own shutdown semantics (graceful SIGTERM vs fast
    SIGINT). Non-callable dispositions (SIG_DFL/SIG_IGN) are left untouched.
    Idempotent; must be called from the main thread.

    Called by the gunicorn ``post_worker_init`` hook (after the worker's own
    ``init_signals()``) and by the ``python app.py`` entry point.
    """
    global _shutdown_hooks_installed
    if _shutdown_hooks_installed:
        return

    def _make_handler(sig_name: str, previous: Any) -> Any:
        def _handler(signum: int, frame: Any) -> None:
            request_shutdown(f"signal:{sig_name}")
            if callable(previous):
                previous(signum, frame)

        return _handler

    for sig in (signal.SIGINT, signal.SIGQUIT, signal.SIGTERM):
        previous = signal.getsignal(sig)
        if not callable(previous):
            continue
        signal.signal(sig, _make_handler(signal.Signals(sig).name, previous))
    _shutdown_hooks_installed = True


def start_background_services() -> None:
    """Start MQTT subscriber and load-management background threads.

    Intentionally NOT called at import time: importing the module must be
    side-effect free so tests and tooling can import it safely. The gunicorn
    entry point (wsgi.py) and the ``__main__`` block call this explicitly
    after the app is constructed.
    """
    if _config.load_tesla_controller == "real":
        _start_mqtt_subscriber()
    if _config.load_manage_enabled is not False:
        _start_load_manager_thread()
    _state.background_services_started_at = datetime.now(timezone.utc)


def create_app() -> Flask:
    """Create and configure the Flask application.

    Builds the app, registers routes, attaches the module-level ``_config``
    singleton to ``app.config["SOLARA_CONFIG"]``, and wires the JSON
    provider, error handler, and Jinja filter. Does NOT start background
    threads — call :func:`start_background_services` explicitly (see
    wsgi.py and the ``__main__`` block).

    Note:
        The ``config`` override parameter was removed: views and background
        services read the module-level ``_config`` singleton, so a per-app
        override was inert and misleading.

    Returns:
        A configured Flask application instance.
    """
    cfg = _config
    application = Flask(__name__)
    application.logger.handlers.clear()
    application.logger.propagate = True
    application.config["SOLARA_CONFIG"] = cfg
    # Register Tesla OAuth routes.
    application.register_blueprint(bp)
    application.jinja_env.filters["astimezonestr"] = astimezone_filter
    application.json = CustomJSONProvider(application)
    application.register_error_handler(RetryableMetricsException, error_retryable)
    application.add_url_rule("/", "index", index)
    application.add_url_rule("/health", "health", health)
    application.add_url_rule("/api/v1/tou", "tou", tou)
    application.add_url_rule("/api/v1/load/status", "load_status", load_status)
    application.add_url_rule("/stream/status", "stream_status", stream_status)
    return application


app = create_app()


logger = app.logger


def _route_root_logging_through_gunicorn() -> None:
    """Route root logging through gunicorn's handlers when available.

    Under gunicorn, the ``gunicorn.error`` logger carries the server's
    configured handlers; reusing them keeps app logs interleaved with
    request logs in the same output stream.  When gunicorn's logger has
    no handlers (tests, scripts, any other embedding of this module),
    this is a no-op so pre-existing logging configuration is preserved
    instead of being silently wiped.
    """
    gunicorn_logger = logging.getLogger("gunicorn.error")
    if not gunicorn_logger.handlers:
        return
    root_logger = logging.getLogger()
    root_logger.handlers = gunicorn_logger.handlers
    root_logger.setLevel(logging.DEBUG if is_debug() else logging.INFO)


if __name__ != "__main__":
    _route_root_logging_through_gunicorn()
    file_handler = _setup_file_logging(_config)
    if file_handler:
        logging.getLogger().addHandler(file_handler)
        logging.getLogger("gunicorn.error").addHandler(file_handler)
else:
    handler = logging.StreamHandler()
    json_mode = str(_config.get("LOG_FORMAT", "text")).lower() == "json"
    if json_mode:
        handler.setFormatter(logfmt.create_formatter(True))
    else:
        handler.setFormatter(logfmt.StructuredFormatter(
            "[%(asctime)s] [%(process)d] [%(levelname)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S %z",
        ))
    logging.basicConfig(handlers=[handler],
                        level=logging.DEBUG if is_debug() else logging.INFO)
    file_handler = _setup_file_logging(_config)
    if file_handler:
        logging.getLogger().addHandler(file_handler)


atexit.register(_shutdown_load_manager)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--pair-plug":
        if len(sys.argv) < 5:
            print(
                "Usage: uv run python app.py --pair-plug <name> <address> <pin>"
            )
            sys.exit(1)

        from load_controllers import pair_homekit_accessory

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
        from load_controllers import tesla_auth_cli

        success = asyncio.run(tesla_auth_cli())
        if success:
            print("Tesla authentication successful.")
            sys.exit(0)
        else:
            print("Tesla authentication failed. Check logs for detail.")
            sys.exit(1)

    elif len(sys.argv) > 1 and sys.argv[1] == "--provision-fleet-telemetry":
        from pathlib import Path
        from load_manager import provision_fleet_telemetry
        from load_models import FleetTelemetryProvisionConfig  # pylint: disable=ungrouped-imports

        if len(sys.argv) < 4:
            print(
                "Usage: uv run python app.py --provision-fleet-telemetry"
                " <server_hostname> <ca_file_path> [server_port]"
            )
            sys.exit(1)

        hostname = sys.argv[2]
        ca_path = Path(sys.argv[3])
        if not ca_path.exists():
            print(f"CA file not found: {ca_path}")
            sys.exit(1)

        port = int(sys.argv[4]) if len(sys.argv) > 4 else 4443
        cfg = FleetTelemetryProvisionConfig(
            server_hostname=hostname,
            ca_file_path=ca_path,
            server_port=port,
            detailedchargestate_interval_sec=_config.tesla_telemetry_detailedchargestate_interval,
        )
        print(f"Provisioning fleet-telemetry for hostname={hostname} …")
        success = provision_fleet_telemetry(cfg)
        if success:
            print("Fleet telemetry provisioning succeeded.")
            sys.exit(0)
        else:
            print("Fleet telemetry provisioning failed. Check logs for detail.")
            sys.exit(1)

    start_background_services()
    install_shutdown_signal_hooks()
    app.run(host="0.0.0.0", port=8000)
