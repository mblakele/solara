"""Device configuration loaded from devices.json.

Provides typed accessor functions for device-specific settings: smart meter,
plugs, and Tesla vehicle config. Secrets (credentials, API keys) remain in
environment variables via decouple.

The file is cached on first load; call reload() to clear the cache.
Returns sensible defaults when the file is missing or malformed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_DEVICES_FILE = Path(__file__).parent / "devices.json"

_cache: dict[str, Any] | None = None


def _load() -> dict[str, Any]:
    """Load and cache devices.json. Returns empty dict if file missing."""
    global _cache
    if _cache is not None:
        return _cache
    try:
        with open(_DEVICES_FILE, encoding="utf-8") as f:
            _cache = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        _cache = {}
    return _cache


def reload() -> None:
    """Clear cache so next access re-reads the file.

    Useful for tests and hot-reload scenarios.
    """
    global _cache
    _cache = None


# === Accessors ===


def get_timezone() -> str:
    """Return configured timezone, defaulting to America/Los_Angeles."""
    return _load().get("timezone", "America/Los_Angeles")


def get_smartmeter_device() -> str:
    """Return the NBC device name (e.g., 'EM1-XXXX')."""
    return _load().get("smartmeter", {}).get("device", "")


def get_target_wh() -> int:
    """Return target Wh per quarter-hour for load decisions."""
    return int(_load().get("smartmeter", {}).get("target_wh", -500))


def get_homekit_plugs() -> list[dict[str, Any]]:
    """Return HomeKit plug entries from devices.json."""
    return _load().get("plugs", {}).get("homekit", [])


def get_vocolinc_plugs() -> list[dict[str, Any]]:
    """Return VOCOlinc plug entries from devices.json."""
    return _load().get("plugs", {}).get("vocolinc", [])


def get_tesla_config() -> dict[str, Any] | None:
    """Return Tesla vehicle section, or None if not configured.

    Returns None when the 'tesla' key is absent or 'vehicle_id' is empty/missing.
    """
    section = _load().get("tesla")
    if not section or not section.get("vehicle_id"):
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


def get_all() -> dict[str, Any]:
    """Return the complete devices.json contents.

    Returns:
        Full configuration dictionary.
    """
    return _load()
