"""Centralized application configuration.

Provides typed accessor methods for all settings, reading from environment
variables (via decouple) and devices.json in a unified interface. Callers
never need to know which source a setting comes from.

Usage:
    cfg = Config()
    timezone_str = cfg.timezone              # env or devices.json fallback
    target_wh = cfg.load_target_wh           # env overrides devices.json
    is_mock = cfg.is_mock_mode               # derived property
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from decouple import UndefinedValueError, config as _decouple_config
import device_config


logger = logging.getLogger(__name__)


class Config:
    """Unified configuration accessor for all Solara settings.

    Reads from environment variables first, falls back to devices.json
    where applicable. Derived properties combine multiple sources.


    """

    def __init__(self) -> None:
        pass

    @property
    def timezone(self) -> str:
        """Return device timezone, evaluated lazily for testability."""
        return _decouple_config("TIMEZONE", default="America/Los_Angeles")

    @property
    def is_mock_mode(self) -> bool:
        """Return True when running in mock/test mode."""
        username = _decouple_config("VUE_USERNAME", default=None)  # type: ignore[assignment]
        return username is None or username == "" or _decouple_config(
            "MOCK", default="False", cast=bool
        )

    @property
    def is_mock_error(self) -> bool:
        """Return True when mock error mode is enabled."""
        return _decouple_config("MOCK_ERROR", default="False", cast=bool)

    @property
    def load_manage_enabled(self) -> bool | str:
        """Return True/False or HH:MM-HH:MM time range for load management.

        Returns False when disabled, True or a string (time-range) when enabled
        via env var LOAD_MANAGE_ENABLED. Falls back to devices.json if unset.

        Time-range strings are returned as-is so _parse_load_manage_enabled
        can extract the start/end times. Boolean strings are converted to bools.
        """
        raw = _decouple_config("LOAD_MANAGE_ENABLED", default=None)  # type: ignore[assignment]
        if raw is not None and isinstance(raw, str):
            lower = raw.lower()
            # Time-range strings (e.g. "06:45-15:00") are returned as-is
            if "-" in raw and not lower == "false":
                return raw
            # Boolean strings are converted to bools
            if lower in ("true", "yes"):
                return True
            # Anything else (including "false") is False
            if lower in ("false", "no"):
                return False
        # Fallback to devices.json smartmeter section
        sm = device_config._load().get("smartmeter", {})  # noqa: W0212
        return bool(sm.get("target_wh") is not None)  # type: ignore[return-value]

    @property
    def load_target_wh(self) -> int:
        """Return target Wh per quarter-hour for load decisions."""
        env_val = _decouple_config("LOAD_TARGET_WH", default=None)
        if env_val is not None:
            return int(env_val)
        return device_config.get_target_wh()

    @property
    def load_manage_interval_secs(self) -> int:
        """Return seconds between load management cycles."""
        return _decouple_config(
            "LOAD_MANAGE_INTERVAL_SECS", default=30, cast=int
        )

    @property
    def load_manage_api_key(self) -> str:
        """Return API key for manual load management endpoint."""
        return _decouple_config("LOAD_MANAGE_API_KEY", default="", cast=str)  # type: ignore[return-value]

    @property
    def load_nbc_device(self) -> str:
        """Return NBC device name for load management."""
        env_val = _decouple_config("LOAD_NBC_DEVICE", default=None)
        if env_val:
            return env_val
        return device_config.get_smartmeter_device()

    @property
    def debug(self) -> bool:
        """Return True when DEBUG mode is enabled."""
        return _decouple_config("DEBUG", default="False", cast=bool)

    @property
    def dry_run(self) -> bool:
        """Return True when load management is in dry-run mode."""
        return _decouple_config("LOAD_MANAGE_DRY_RUN", default="True", cast=bool)

    @property
    def vue_username(self) -> Optional[str]:
        """Return Emporia VUE username, or None if not configured."""
        return _decouple_config("VUE_USERNAME", default=None)

    @property
    def vue_password(self) -> Optional[str]:
        """Return Emporia VUE password, or None if not configured."""
        return _decouple_config("VUE_PASSWORD", default=None)

    @property
    def tesla_client_id(self) -> Optional[str]:
        """Return Tesla Fleet API client ID, or None if not configured."""
        try:
            return _decouple_config("TESLA_CLIENT_ID", default=None)  # type: ignore[return-value]
        except UndefinedValueError:
            return None

    @property
    def tesla_client_secret(self) -> Optional[str]:
        """Return Tesla Fleet API client secret, or None if not configured."""
        try:
            return _decouple_config("TESLA_CLIENT_SECRET", default=None)  # type: ignore[return-value]
        except UndefinedValueError:
            return None

    @property
    def tesla_private_key_path(self) -> Optional[str]:
        """Return Tesla private key file path, or None."""
        return _decouple_config("TESLA_PRIVATE_KEY_PATH", default=None)

    @property
    def tesla_redirect_uri(self) -> str:
        """Return Tesla OAuth redirect URI."""
        return _decouple_config("TESLA_REDIRECT_URI", default="", cast=str)  # type: ignore[return-value]

    @property
    def tesla_region(self) -> str:
        """Return Tesla API region."""
        return _decouple_config("TESLA_REGION", default="na", cast=str)  # type: ignore[return-value]

    @property
    def load_plug_controller(self) -> str:
        """Return plug controller type (real or stub)."""
        return _decouple_config("LOAD_PLUG_CONTROLLER", default="stub", cast=str).lower()

    @property
    def load_tesla_controller(self) -> str:
        """Return Tesla controller type (real or stub)."""
        return _decouple_config("LOAD_TESLA_CONTROLLER", default="stub", cast=str).lower()

    @property
    def vocolinc_username(self) -> str:
        """Return VOCOlinc username, or empty string."""
        return _decouple_config("VOCOLINC_USERNAME", default="", cast=str).strip()  # type: ignore[return-value]

    @property
    def vocolinc_password(self) -> str:
        """Return VOCOlinc password, or empty string."""
        return _decouple_config("VOCOLINC_PASSWORD", default="", cast=str).strip()  # type: ignore[return-value]

    def get_homekit_plugs(self) -> list[dict[str, Any]]:
        """Return HomeKit plug entries from devices.json."""
        return device_config.get_homekit_plugs()

    def get_vocolinc_plugs(self) -> list[dict[str, Any]]:
        """Return VOCOlinc plug entries from devices.json."""
        return device_config.get_vocolinc_plugs()

    def get_tesla_config(self) -> dict[str, Any] | None:
        """Return Tesla vehicle config section from devices.json, or None."""
        return device_config.get_tesla_config()

    def get_plugins(self) -> dict[str, Any]:
        """Return all plug configuration from devices.json."""
        return device_config._load().get("plugs", {})  # noqa: W0212

    def get_all(self) -> dict[str, Any]:
        """Return full devices.json contents as a dict."""
        return device_config._load()  # noqa: W0212

    def reload(self) -> None:
        """Clear all cached configuration."""
        device_config.reload()


# Module-level singleton for convenience (backward compatible)
cfg = Config()


def get_timezone() -> str:
    """Return configured timezone — backward compatible alias."""
    return cfg.timezone


def reload() -> None:
    """Reload configuration — backward compatible alias."""
    cfg.reload()
