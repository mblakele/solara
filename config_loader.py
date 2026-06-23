"""Configuration parsing helpers for Solara.

Extracted from load_manager.py to reduce its size and improve testability
of config parsing logic. Re-exported from load_manager for backward compat.

Functions in this module read configuration from environment variables
(via the centralized Config class) and devices.json, providing a unified
interface for all load management configuration needs.
"""

from __future__ import annotations

import logging
import re
from datetime import time
from typing import Any

import device_config

from config import Config, _config


logger = logging.getLogger(__name__)


# === Time-range parsing helpers ===

_TIME_RANGE_RE = re.compile(r"^(\d{1,2}:\d{2})-(\d{1,2}:\d{2})$")


def _parse_time(value: str) -> time:
    """Parse a HH:MM string into a datetime.time object.

    Args:
        value: Time string in HH:MM 24-hour format.

    Returns:
        A timezone-naive time object.

    Raises:
        ValueError: If the string is not a valid HH:MM time.
    """
    parts = value.split(":")
    if len(parts) != 2:
        raise ValueError(f"Invalid time format: {value!r}, expected HH:MM")
    hour, minute = int(parts[0]), int(parts[1])
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(
            f"Time out of range: {value!r}, hour must be 0-23, minute 0-59"
        )
    return time(hour=hour, minute=minute)


def _parse_load_manage_enabled(value: bool | str) -> bool | tuple[time, time]:
    """Parse LOAD_MANAGE_ENABLED value.

    Accepts "True", "False" (case-insensitive), or a time range in
    HH:MM-HH:MM 24-hour format (e.g., "06:45-15:00"). A time range means
    load management is active only during that window (inclusive start,
    exclusive end). Also accepts Python bool values directly.

    Args:
        value: Raw string from the environment variable, or a Python bool
            when coming through Config.load_manage_enabled.

    Returns:
        True if always enabled, False if never enabled, or a tuple of
        (start_time, end_time) for time-range mode.

    Raises:
        ValueError: If the value doesn't match any recognized format.
    """
    # Handle Python bool values directly (from Config.load_manage_enabled).
    if isinstance(value, bool):
        return value

    if value.strip().lower() in ("true", "1", "yes"):
        return True
    if value.strip().lower() in ("false", "0", "no", ""):
        return False

    match = _TIME_RANGE_RE.match(value.strip())
    if match is not None:
        start = _parse_time(match.group(1))
        end = _parse_time(match.group(2))
        return (start, end)

    raise ValueError(
        f"Invalid LOAD_MANAGE_ENABLED value: {value!r}. "
        'Expected True/False or a time range like "06:45-15:00"'
    )


def _parse_device_time_range(value: str | None) -> tuple[time, time] | None:
    """Parse a device-level time range string like '10:00-15:00'.

    Args:
        value: Raw string from devices.json, or None if not configured.

    Returns:
        Tuple of (start_time, end_time) or None if not configured.
        Logs warning and returns None on parse error (device loads without
        time restriction).
    """
    if value is None:
        return None
    match = _TIME_RANGE_RE.match(value.strip())
    if match is None:
        logger.warning("Invalid device time range %r, ignoring", value)
        return None
    return (_parse_time(match.group(1)), _parse_time(match.group(2)))


# === Config loading functions ===


def load_plugs_from_file() -> dict[str, Any]:
    """Load HomeKit plug configurations from devices.json.

    Returns:
        Dict mapping plug names to PlugConfig objects.
    """
    from load_models import PlugConfig

    plugs: dict[str, PlugConfig] = {}
    for entry in device_config.get_homekit_plugs():
        name = entry["name"].lower()
        plugs[name] = PlugConfig(
            name=name,
            accessory_id=entry["accessory_id"],
            power_watts=float(entry["power_watts"]) if entry.get("power_watts") is not None else None,
            priority=int(entry.get("priority", 0)),
            time_range=_parse_device_time_range(entry.get("time_range")),
            sentinel=bool(entry.get("sentinel", False)),
        )
    return plugs


def load_vocolinc_credentials(config: Config | None = None) -> tuple[str, str] | None:
    """Load VOCOlinc username and password from environment variables.

    Args:
        config: Optional Config instance. Falls back to module-level
            singleton when None.

    Returns:
        Tuple of (username, password) or None if not configured.
    """
    cfg = config if config is not None else _config
    username = cfg.vocolinc_username
    password = cfg.vocolinc_password
    if username and password:
        return (username, password)
    return None


def load_vocolinc_plugs_from_file() -> dict[str, Any]:
    """Load VOCOlinc plug configurations from devices.json.

    Returns:
        Dict mapping plug names to PlugConfig objects.
    """
    from load_models import PlugConfig

    plugs: dict[str, PlugConfig] = {}
    for entry in device_config.get_vocolinc_plugs():
        name = entry["name"].lower()
        plugs[name] = PlugConfig(
            name=name,
            accessory_id=entry["device_name"],
            power_watts=float(entry["power_watts"]) if entry.get("power_watts") is not None else None,
            priority=int(entry.get("priority", 0)),
            controller_type="vocolinc",
            time_range=_parse_device_time_range(entry.get("time_range")),
            sentinel=bool(entry.get("sentinel", False)),
        )
    return plugs


def load_tesla_config(config: Config | None = None) -> Any:  # type: ignore[return-value]
    """Load Tesla configuration.

    Secrets and device identifiers (client_id, client_secret, vehicle_id,
    redirect_uri) come from environment variables via decouple.
    Home coordinates (home_lat, home_lon) are optional — when both are
    provided the decision engine will gate Tesla charging on the vehicle
            being at home. When either is missing, Tesla charging is allowed
            regardless of location.

    Non-secret device defaults (home_radius_m, charge limits, time_range)
    come from devices.json.

    Args:
        config: Optional Config instance. Falls back to module-level
            singleton when None.

    Returns:
        TeslaConfig if vehicle_id, client_id, and client_secret are present,
        or None if any of those required fields are missing.
        home_lat/home_lon will be None when not configured.
    """
    from load_models import TeslaConfig

    cfg = config if config is not None else _config

    vehicle_id = cfg.tesla_vehicle_id
    if not vehicle_id:
        return None

    client_id = cfg.tesla_client_id
    if not client_id:
        return None

    private_key_path = cfg.tesla_private_key_path or None

    vehicle_command_proxy_url = cfg.tesla_vehicle_command_proxy_url or None

    client_secret: str | None = cfg.tesla_client_secret
    if not client_secret:
        return None

    redirect_uri = cfg.tesla_redirect_uri
    if not redirect_uri:
        return None

    home_lat = cfg.tesla_home_lat
    home_lon = cfg.tesla_home_lon

    dc = device_config.get_tesla_config()
    home_radius_m = 500.0
    charge_amps_min = 5
    charge_amps_max = 48
    time_range = None
    if dc is not None:
        home_radius_m = float(dc.get("home_radius_m", 500))
        charge_amps_min = int(dc.get("charge_amps_min", 5))
        charge_amps_max = int(dc.get("charge_amps_max", 48))
        time_range = _parse_device_time_range(dc.get("time_range"))

    return TeslaConfig(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
        vehicle_id=vehicle_id,
        home_lat=home_lat,
        home_lon=home_lon,
        home_radius_m=home_radius_m,
        charge_amps_min=charge_amps_min,
        charge_amps_max=charge_amps_max,
        private_key_path=private_key_path,
        time_range=time_range,
        vehicle_command_proxy_url=vehicle_command_proxy_url,
    )
