"""Device configuration loaded from devices.json.

Provides typed accessor functions for device-specific settings: smart meter,
plugs, and Tesla vehicle config. Secrets (credentials, API keys) remain in
environment variables via decouple.

The file is cached on first load; call reload() to clear the cache.
Returns sensible defaults when the file is missing.  Raises
DeviceConfigError when the file exists but is malformed or empty.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

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
    except OSError:
        # Permission denied, encoding error, etc.
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

    unmatched: list[str] = []
    for device_name in devices:
        if device_name.lower() not in plug_names:
            unmatched.append(device_name)

    if unmatched:
        sorted_plugs = sorted(plug_names)
        raise DeviceConfigError(
            f"Invalid telegram.devices: {unmatched} not found in plugs. "
            f"Found plug names: {sorted_plugs}."
        )


def _validate_integrity(config: dict[str, Any]) -> None:
    """Run all integrity checks on the freshly parsed config.

    Currently validates that telegram.devices keys reference valid plugs.
    Called automatically by _load() on every successful parse.

    Args:
        config: The parsed devices.json config dict.
    """
    telegram_section = config.get("telegram")
    if telegram_section:
        validate_telegram_devices(config, telegram_section)


# === Accessors ===


def get_timezone() -> str:
    """Return configured timezone, defaulting to America/Los_Angeles."""
    return _load().get("timezone", "America/Los_Angeles")


def get_smartmeter_device() -> str:
    """Return the NBC device name (e.g., 'EM1-XXXX')."""
    return _load().get("smartmeter", {}).get("device", "")


def get_target_wh() -> int:
    """Return target Wh per quarter-hour for load decisions."""
    return int(_load().get("smartmeter", {}).get("target_wh", -50))


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
    """Return True when a smartmeter section with target_wh exists."""
    return bool(_load().get("smartmeter", {}).get("target_wh") is not None)


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
