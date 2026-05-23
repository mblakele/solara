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

from config import cfg as _cfg


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


def load_vocolinc_credentials() -> tuple[str, str] | None:
    """Load VOCOlinc username and password from environment variables.

    Returns:
        Tuple of (username, password) or None if not configured.
    """
    username = _cfg.vocolinc_username
    password = _cfg.vocolinc_password
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


def load_tesla_config() -> Any:  # type: ignore[return-value]
    """Load Tesla configuration.

    Secrets (client_id, client_secret) come from environment variables via
    decouple. Device settings (vehicle_id, GPS coords, charge limits) come
    from devices.json.

    Returns:
        TeslaConfig if fully configured, or None if required fields are missing.
    """
    from load_models import TeslaConfig

    dc = device_config.get_tesla_config()
    if dc is None:
        return None

    client_id = _cfg.tesla_client_id
    if not client_id:
        return None

    private_key_path = _cfg.tesla_private_key_path or None

    client_secret: str | None = _cfg.tesla_client_secret
    if not client_secret:
        return None

    return TeslaConfig(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=dc["redirect_uri"],
        vehicle_id=dc["vehicle_id"],
        home_lat=float(dc.get("home_lat", 0)),
        home_lon=float(dc.get("home_lon", 0)),
        home_radius_m=float(dc.get("home_radius_m", 500)),
        charge_amps_min=int(dc.get("charge_amps_min", 5)),
        charge_amps_max=int(dc.get("charge_amps_max", 48)),
        private_key_path=private_key_path,
        time_range=_parse_device_time_range(dc.get("time_range")),
    )
