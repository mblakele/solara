"""
Flask application providing energy usage metrics and TOU reporting.

Provides endpoints for real-time energy metrics and historical
Time-of-Use aggregation from Emporia VUE API.
"""

import logging

from datetime import datetime

import requests
from decouple import config
from flask import Flask, Response, abort, make_response, render_template, request
import pytz

from metrics import Metrics, MetricsMock, TOUReporter, RetryableMetricsException
from util import CustomJSONProvider, TIMEZONE

# global setup
app = Flask(__name__)


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
    elif isinstance(obj, list):
        return [camelize(i) for i in obj]
    return obj


# The template_folder and static_folder default to 'templates' and 'static'
# relative to the application path. Using the default root structure.
DEBUG = config("DEBUG", False, cast=bool)
logger = app.logger
logger.setLevel(logging.DEBUG if DEBUG else logging.INFO)


def astimezone_filter(dt: datetime, tz_str: str) -> datetime:
    """Convert datetime to specified timezone for Jinja2 template filter."""
    tz = pytz.timezone(tz_str)
    return dt.astimezone(tz)


def parse_date_to_utc(date_str: str) -> datetime:
    """Parse date string and convert to UTC timezone."""
    tz = pytz.timezone(TIMEZONE)
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
    """Return TOUReporter or realistic mock values based on configuration.

    Raises requests.exceptions.HTTPError or IOError from TOUReporter.
    Returns a dict with realistic non-zero bucket values in mock mode.
    """
    is_mock = (
        config("VUE_USERNAME", None) is None
        or force_mock
        or config("MOCK", default="False", cast=bool)
    )
    if is_mock:
        return MetricsMock().tou_result
    model = TOUReporter(start_date, end_date, logger)
    return model.tou_result


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


def _json_response(payload: dict, app: Flask) -> Response:
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
def index() -> Response:
    """Main index endpoint serving HTML or JSON based on Accept header."""
    logger.debug("index")
    is_mock_error = config("MOCK_ERROR", default="False", cast=bool)
    force_mock = config("MOCK", default="False", cast=bool)

    instant_minute = request.args.get("instant_minute")
    if instant_minute is not None and force_mock:
        try:
            instant_minute = int(instant_minute)
        except (ValueError, TypeError):
            instant_minute = None
    model = _get_model(logger, is_mock_error, force_mock, instant_minute=instant_minute)

    # check for default html first, to handle missing Accept header.
    if request.accept_mimetypes.accept_html:
        return render_template("index.html", metrics=model.metrics)

    if request.accept_mimetypes.accept_json:
        payload = camelize(model.metrics)
        return _json_response(payload, app)

    return abort(406)


@app.route("/health")
def health() -> Response:
    """Health check endpoint returning 'ok'."""
    logger.debug("health")
    resp = Response("ok")
    resp.headers["Content-Type"] = "text/plain"
    return resp


@app.route("/api/v1/tou")
def tou() -> Response:
    """Time-of-Use API endpoint for energy consumption data."""
    logger.debug("tou")

    start_date_str = request.args.get("start_date")
    end_date_str = request.args.get("end_date")

    result = _validate_dates(start_date_str, end_date_str)
    if isinstance(result, Response):
        return result

    start_date, end_date = result

    try:
        buckets = _get_tou_model(start_date, end_date)
    except (requests.exceptions.HTTPError, IOError) as e:
        error_msg = str(e)
        if isinstance(e, requests.exceptions.HTTPError) and e.response is not None:
            try:
                error_msg = f"{error_msg}: {e.response.text}"
            except (requests.exceptions.RequestException, AttributeError):
                pass
        logger.error("TOU error: %s", error_msg)
        return abort(500, f"Error fetching usage data: {error_msg}")

    if request.accept_mimetypes.accept_html:
        return render_template(
            "tou.html",
            start_date=start_date_str,
            end_date=end_date_str,
            buckets=buckets,
        )

    payload = {
        "start_date": start_date_str,
        "end_date": end_date_str,
        "buckets": buckets,
    }
    return _json_response(payload, app)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
