"""Device configuration loaded from devices.json.

Provides typed accessor functions for device-specific settings: smart meter,
plugs, and Tesla vehicle config. Secrets (credentials, API keys) remain in
environment variables (via the config module).

The file is cached on first load; call reload() to clear the cache.
Returns sensible defaults when the file is missing.  Raises
DeviceConfigError when the file exists but is malformed or empty.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import pytz

logger = logging.getLogger(__name__)

_DEVICES_FILE = Path(__file__).parent / "devices.json"

_cache: dict[str, Any] | None = None


class DeviceConfigError(Exception):
    """Raised when devices.json exists but is empty or malformed.

    This exception prevents the application from starting with invalid
    device configuration, helping catch misconfiguration early.
    """


def _load() -> dict[str, Any]:
    """Load and cache devices.json. Returns empty dict if file missing.

    Raises:
        DeviceConfigError: If the file exists but is empty or contains
            invalid JSON.
    """
    global _cache
    if _cache is not None:
        return _cache
    try:
        text = _DEVICES_FILE.read_text(encoding="utf-8")
    except FileNotFoundError:
        _cache = {}
        return _cache
    except OSError as e:
        # Permission denied, encoding error, etc. — distinguishable from a
        # missing file only if we say so.
        logger.error("Failed to read devices.json: %s", e, exc_info=True)
        _cache = {}
        return _cache

    if not text.strip():
        raise DeviceConfigError(
            "devices.json exists but is empty. "
            "Copy devices.json.example to devices.json and configure it."
        )

    try:
        _cache = json.loads(text)
    except json.JSONDecodeError as e:
        raise DeviceConfigError(
            "devices.json exists but contains invalid JSON. "
            f"Parsing error: {e}"
        ) from e

    # Integrity checks — run after every successful parse.
    _validate_integrity(_cache)

    return _cache


def reload() -> None:
    """Clear cache so next access re-reads the file.

    Useful for tests and hot-reload scenarios.
    """
    global _cache
    _cache = None


# === Target Wh band and TOU schedule ===

_DEFAULT_TARGET_WH = -50
_CLOCK_RE = re.compile(r"^(\d{1,2}):(\d{2})$")


@dataclass(frozen=True)
class TargetWhSetting:
    """Resolved target for one moment in the configured timezone.

    Attributes:
        target_wh: The aim point used for gap and budget arithmetic.
        low_wh: Lower bound of the acceptable band, or None.
        high_wh: Upper bound of the acceptable band, or None.
        source: Which configuration form supplied the setting.
    """

    target_wh: int
    low_wh: int | None = None
    high_wh: int | None = None
    source: str = "default"

    @property
    def band(self) -> tuple[int, int] | None:
        """Return the acceptable band as ``(low, high)``, or None."""
        if self.low_wh is None or self.high_wh is None:
            return None
        return (self.low_wh, self.high_wh)


@dataclass(frozen=True)
class _TargetWindow:
    """One parsed time-of-use window, in minutes since local midnight."""

    start: int
    end: int
    target_wh: int | None
    low_wh: int | None
    high_wh: int | None


@dataclass(frozen=True)
class _TargetConfig:
    """Parsed smartmeter target keys before time resolution."""

    target_wh: int | None
    low_wh: int | None
    high_wh: int | None
    windows: tuple[_TargetWindow, ...]


def _parse_int(value: Any, field: str) -> int:
    """Parse an integral JSON number, rejecting bools and fractions."""
    if isinstance(value, bool):
        raise DeviceConfigError(f"{field} must be an integer, got {value!r}")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    raise DeviceConfigError(f"{field} must be an integer, got {value!r}")


def _parse_clock(value: Any, field: str) -> int:
    """Parse ``'HH:MM'`` into minutes since midnight."""
    if not isinstance(value, str):
        raise DeviceConfigError(
            f"{field} must be a clock string like '15:00', got {value!r}"
        )
    match = _CLOCK_RE.match(value.strip())
    if match is None:
        raise DeviceConfigError(
            f"{field} must be a clock string like '15:00', got {value!r}"
        )
    hour = int(match.group(1))
    minute = int(match.group(2))
    if hour > 23 or minute > 59:
        raise DeviceConfigError(
            f"{field} has an out-of-range clock value {value!r}"
        )
    return hour * 60 + minute


def _parse_time_range(value: Any, field: str) -> tuple[int, int]:
    """Parse ``'HH:MM-HH:MM'`` into a half-open ``[start, end)`` pair.

    ``to: "00:00"`` means the end of the day (24:00). Windows must not wrap
    past midnight.
    """
    if not isinstance(value, str) or "-" not in value:
        raise DeviceConfigError(
            f"{field} must look like 'HH:MM-HH:MM', got {value!r}"
        )
    start_text, end_text = value.split("-", 1)
    start = _parse_clock(start_text, field)
    end = _parse_clock(end_text, field)
    if start == end:
        raise DeviceConfigError(
            f"{field} must span at least one minute: {value!r}"
        )
    if end == 0:
        end = 24 * 60
    if start > end:
        raise DeviceConfigError(
            f"{field} must not wrap past midnight: {value!r}"
        )
    return start, end


def _parse_band(value: Any, field: str) -> tuple[int, int]:
    """Parse ``[low, high]`` with ``low <= high``."""
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise DeviceConfigError(
            f"{field} must be [low, high], got {value!r}"
        )
    low = _parse_int(value[0], field)
    high = _parse_int(value[1], field)
    if low > high:
        raise DeviceConfigError(
            f"{field} low ({low}) must be <= high ({high})"
        )
    return low, high


def _parse_schedule(value: Any) -> tuple[_TargetWindow, ...]:
    """Parse and validate the time-of-use window list."""
    field = "target_wh_schedule"
    if not isinstance(value, list):
        raise DeviceConfigError(f"{field} must be a list of windows")
    windows: list[_TargetWindow] = []
    for entry in value:
        if not isinstance(entry, dict):
            raise DeviceConfigError(
                f"{field} entries must be objects, got {entry!r}"
            )
        if "time_range" not in entry:
            raise DeviceConfigError(f"{field} entries need time_range")
        start, end = _parse_time_range(
            entry["time_range"], f"{field}.time_range"
        )
        target = None
        if "target_wh" in entry:
            target = _parse_int(entry["target_wh"], f"{field}.target_wh")
        band = None
        if "target_wh_range" in entry:
            band = _parse_band(
                entry["target_wh_range"], f"{field}.target_wh_range"
            )
        if target is None and band is None:
            raise DeviceConfigError(
                f"{field} entries need target_wh or target_wh_range"
            )
        low, high = band if band is not None else (None, None)
        windows.append(_TargetWindow(start, end, target, low, high))

    ordered = tuple(sorted(windows, key=lambda window: window.start))
    for previous, current in zip(ordered, ordered[1:]):
        if current.start < previous.end:
            raise DeviceConfigError(
                f"{field} windows overlap: "
                f"{previous.start}-{previous.end} and "
                f"{current.start}-{current.end}"
            )
    return ordered


def _parse_target_config(section: dict[str, Any]) -> _TargetConfig:
    """Parse and validate the three target keys in a smartmeter section."""
    target = None
    if "target_wh" in section:
        target = _parse_int(section["target_wh"], "target_wh")
    low = high = None
    if "target_wh_range" in section:
        low, high = _parse_band(section["target_wh_range"], "target_wh_range")
    windows: tuple[_TargetWindow, ...] = ()
    if "target_wh_schedule" in section:
        windows = _parse_schedule(section["target_wh_schedule"])
    return _TargetConfig(target, low, high, windows)


def _local_minutes(now: datetime | None) -> int:
    """Return the configured-timezone clock time as minutes since midnight."""
    local_tz = pytz.timezone(get_timezone())
    if now is None:
        now = datetime.now(local_tz)
    elif now.tzinfo is None:
        now = local_tz.localize(now)
    local = now.astimezone(local_tz)
    return local.hour * 60 + local.minute


def _midpoint(low: int, high: int) -> int:
    """Return the band midpoint, rounded to the nearest integer."""
    return int(round((low + high) / 2))


def _aim(target: int | None, low: int | None, high: int | None) -> int:
    """Choose the aim point for a resolved target."""
    if target is not None:
        return target
    if low is not None and high is not None:
        return _midpoint(low, high)
    return _DEFAULT_TARGET_WH


def get_target_wh_setting(now: datetime | None = None) -> TargetWhSetting:
    """Resolve the target Wh setting for the local time *now*.

    Args:
        now: Aware datetime to resolve. None uses the current local time.

    Returns:
        The resolved aim and optional acceptable band.
    """
    section = _load().get("smartmeter", {})
    if not isinstance(section, dict):
        section = {}
    parsed = _parse_target_config(section)
    minutes = _local_minutes(now)

    active = next(
        (
            window
            for window in parsed.windows
            if window.start <= minutes < window.end
        ),
        None,
    )
    if active is None:
        low, high = parsed.low_wh, parsed.high_wh
        if low is not None:
            source = "target_wh_range"
        elif parsed.target_wh is not None:
            source = "target_wh"
        else:
            source = "default"
        return TargetWhSetting(
            _aim(parsed.target_wh, low, high), low, high, source
        )

    low = active.low_wh if active.low_wh is not None else parsed.low_wh
    high = active.high_wh if active.high_wh is not None else parsed.high_wh
    # A band-only window aims at its own midpoint, even when the top level
    # also names a scalar. The top-level scalar is the gap fallback only.
    target: int | None
    if active.target_wh is not None:
        target = active.target_wh
    elif active.low_wh is not None and active.high_wh is not None:
        target = _midpoint(active.low_wh, active.high_wh)
    else:
        target = parsed.target_wh
    return TargetWhSetting(
        _aim(target, low, high), low, high, "target_wh_schedule"
    )


def get_target_wh(now: datetime | None = None) -> int:
    """Return the resolved target Wh per quarter-hour."""
    return get_target_wh_setting(now).target_wh


def get_target_wh_band(now: datetime | None = None) -> tuple[int, int] | None:
    """Return the resolved acceptable Wh band, or None."""
    return get_target_wh_setting(now).band


# === Integrity validation ===


def validate_telegram_devices(
    config: dict[str, Any],
    telegram_config: dict[str, Any],
) -> None:
    """Validate that telegram.devices entries match plug names.

    Checks every key in ``telegram_config["devices"]`` against the combined
    plug names from ``config["plugs"]["homekit"]`` and
    ``config["plugs"]["vocolinc"]``.  Comparison is case-insensitive to
    match the convention used by the plug loaders.

    The special device name "tesla" (case-insensitive) is always accepted
    to enable Tesla stop-charging alerts via the telegram.devices whitelist.

    Args:
        config: The full devices.json config dict (contains plugs section).
        telegram_config: The telegram section from devices.json.

    Raises:
        DeviceConfigError: If any telegram device has no matching plug.
    """
    if not telegram_config:
        return

    devices = telegram_config.get("devices")
    if not devices:
        return

    # Collect all known plug names, lowercased.
    plug_names: set[str] = set()
    for plug_type in ("homekit", "vocolinc"):
        for entry in config.get("plugs", {}).get(plug_type, []):
            name = entry.get("name", "")
            if name:
                plug_names.add(name.lower())

    # "tesla" is a valid device name for Tesla stop-charging alerts.
    plug_names.add("tesla")

    unmatched: list[str] = []
    for device_name in devices:
        if device_name.lower() not in plug_names:
            unmatched.append(device_name)

    if unmatched:
        sorted_plugs = sorted(plug_names - {"tesla"})
        raise DeviceConfigError(
            f"Invalid telegram.devices: {unmatched} not found in plugs. "
            f"Found plug names: {sorted_plugs}."
        )


def _validate_integrity(config: dict[str, Any]) -> None:
    """Run all integrity checks on the freshly parsed config.

    Currently validates telegram.devices keys and the smartmeter target
    forms. Called automatically by _load() on every successful parse.

    Args:
        config: The parsed devices.json config dict.
    """
    telegram_section = config.get("telegram")
    if telegram_section:
        validate_telegram_devices(config, telegram_section)
    smartmeter_section = config.get("smartmeter")
    if isinstance(smartmeter_section, dict):
        _parse_target_config(smartmeter_section)


# === Accessors ===


def get_timezone() -> str:
    """Return configured timezone, defaulting to America/Los_Angeles."""
    return _load().get("timezone", "America/Los_Angeles")


def get_smartmeter_device() -> str:
    """Return the NBC device name (e.g., 'EM1-XXXX')."""
    return _load().get("smartmeter", {}).get("device", "")


def get_homekit_plugs() -> list[dict[str, Any]]:
    """Return HomeKit plug entries from devices.json."""
    return _load().get("plugs", {}).get("homekit", [])


def get_vocolinc_plugs() -> list[dict[str, Any]]:
    """Return VOCOlinc plug entries from devices.json."""
    return _load().get("plugs", {}).get("vocolinc", [])


def get_tesla_config() -> dict[str, Any] | None:
    """Return Tesla vehicle section, or None if not configured.

    Returns the section whenever a 'tesla' key exists, regardless of
    whether 'vehicle_id' is present.  vehicle_id may be configured via
    environment variables (TESLA_VEHICLE_ID) rather than devices.json,
    but amp limits and other device-level settings still come from the
    file and must be readable.

    Returns None when the 'tesla' key is absent entirely.
    """
    load = _load()
    if load is None:
        return None
    section = load.get("tesla")
    if not section:
        return None
    return section


def has_smartmeter() -> bool:
    """Return True when the smartmeter section declares any target form."""
    section = _load().get("smartmeter", {})
    if not isinstance(section, dict):
        return False
    return any(
        key in section
        for key in ("target_wh", "target_wh_range", "target_wh_schedule")
    )


def get_all_plugs() -> dict[str, Any]:
    """Return the full plugs section from devices.json.

    Returns:
        Dictionary with 'homekit' and/or 'vocolinc' lists.
    """
    return _load().get("plugs", {})


def get_telegram_config() -> dict[str, Any] | None:
    """Return telegram section from devices.json, or None if absent.

    Returns None when the 'telegram' key is absent or 'enabled' is falsy.

    Returns:
        The telegram configuration dict, or None.
    """
    section = _load().get("telegram")
    if not section:
        return None
    return section


def get_all() -> dict[str, Any]:
    """Return the complete devices.json contents.

    Returns:
        Full configuration dictionary.
    """
    return _load()
