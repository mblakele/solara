"""Centralized application configuration.

Provides typed accessor methods for all settings, reading from environment
variables (via decouple) and devices.json in a unified interface. Callers
never need to know which source a setting comes from.

Usage:
    _config = Config()
    timezone_str = _config.timezone              # env or devices.json fallback
    target_wh = _config.load_target_wh           # env overrides devices.json
    is_mock = _config.is_mock_mode               # derived property
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from decouple import UndefinedValueError, config as _decouple_config
import device_config


logger = logging.getLogger(__name__)


class Config:
    """Unified configuration accessor for all Solara settings.

    Reads from environment variables first, falls back to devices.json
    where applicable. Derived properties combine multiple sources.

    Args:
        overrides: Optional dict of key→value pairs that take precedence
            over env vars and devices.json. Used by tests to inject config
            without patching ``_decouple_config`` or ``_cfg``.
    """

    def __init__(self, overrides: dict[str, str] | None = None) -> None:
        self._overrides = overrides or {}

    def _get(self, key: str, default: str | None = None) -> str | None:
        """Check overrides first, then fall back to decouple."""
        if key in self._overrides:
            return self._overrides[key]
        return _decouple_config(key, default=default)  # type: ignore[return-value]

    def _get_bool(self, key: str, default: str = "False") -> bool:
        """Return boolean config value, checking overrides first."""
        if key in self._overrides:
            raw = self._overrides[key]
            return raw.lower() in ("true", "yes", "1", "t")
        return _decouple_config(key, default=default, cast=bool)  # type: ignore[return-value]

    def _get_int(self, key: str, default: int = 0) -> int:
        """Return integer config value, checking overrides first."""
        if key in self._overrides:
            return int(self._overrides[key])
        return _decouple_config(key, default=default, cast=int)  # type: ignore[return-value]

    @property
    def timezone(self) -> str:
        """Return device timezone, evaluated lazily for testability."""
        val = self._get("TIMEZONE", default="America/Los_Angeles")
        assert val is not None
        return val

    @property
    def is_mock_mode(self) -> bool:
        """Return True when running in mock/test mode."""
        return self._get_bool("MOCK")

    @property
    def is_mock_error(self) -> bool:
        """Return True when mock error mode is enabled."""
        return self._get_bool("MOCK_ERROR")

    @property
    def load_manage_enabled(self) -> bool | str:
        """Return True/False or HH:MM-HH:MM time range for load management.

        Returns False when disabled, True or a string (time-range) when enabled
        via env var LOAD_MANAGE_ENABLED. Falls back to devices.json if unset.

        Time-range strings are returned as-is so _parse_load_manage_enabled
        can extract the start/end times. Boolean strings are converted to bools.
        """
        if "LOAD_MANAGE_ENABLED" in self._overrides:
            raw = self._overrides["LOAD_MANAGE_ENABLED"]
        else:
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
        return device_config.has_smartmeter()

    @property
    def load_target_wh(self) -> int:
        """Return target Wh per quarter-hour for load decisions."""
        if "LOAD_TARGET_WH" in self._overrides:
            return int(self._overrides["LOAD_TARGET_WH"])
        env_val = _decouple_config("LOAD_TARGET_WH", default=None)
        if env_val is not None:
            return int(env_val)
        return device_config.get_target_wh()

    @property
    def load_manage_interval_secs(self) -> int:
        """Return seconds between load management cycles."""
        return self._get_int("LOAD_MANAGE_INTERVAL_SECS", default=30)

    @property
    def load_manage_api_key(self) -> str:
        """Return API key for manual load management endpoint."""
        if "LOAD_MANAGE_API_KEY" in self._overrides:
            return self._overrides["LOAD_MANAGE_API_KEY"]
        return _decouple_config("LOAD_MANAGE_API_KEY", default="", cast=str)  # type: ignore[return-value]

    @property
    def load_nbc_device(self) -> str:
        """Return NBC device name for load management."""
        if "LOAD_NBC_DEVICE" in self._overrides:
            return self._overrides["LOAD_NBC_DEVICE"]
        env_val = _decouple_config("LOAD_NBC_DEVICE", default=None)
        if env_val:
            return env_val
        return device_config.get_smartmeter_device()

    @property
    def debug(self) -> bool:
        """Return True when DEBUG mode is enabled."""
        return self._get_bool("DEBUG")

    @property
    def dry_run(self) -> bool:
        """Return True when load management is in dry-run mode."""
        return self._get_bool("LOAD_MANAGE_DRY_RUN")

    @property
    def vue_username(self) -> Optional[str]:
        """Return Emporia VUE username, or None if not configured."""
        if "VUE_USERNAME" in self._overrides:
            val = self._overrides["VUE_USERNAME"]
            return val if val else None
        return _decouple_config("VUE_USERNAME", default=None)

    @property
    def vue_password(self) -> Optional[str]:
        """Return Emporia VUE password, or None if not configured."""
        if "VUE_PASSWORD" in self._overrides:
            val = self._overrides["VUE_PASSWORD"]
            return val if val else None
        return _decouple_config("VUE_PASSWORD", default=None)

    @property
    def tesla_client_id(self) -> Optional[str]:
        """Return Tesla Fleet API client ID, or None if not configured."""
        if "TESLA_CLIENT_ID" in self._overrides:
            return self._overrides["TESLA_CLIENT_ID"] or None
        try:
            return _decouple_config("TESLA_CLIENT_ID", default=None)  # type: ignore[return-value]
        except UndefinedValueError:
            return None

    @property
    def tesla_client_secret(self) -> Optional[str]:
        """Return Tesla Fleet API client secret, or None if not configured."""
        if "TESLA_CLIENT_SECRET" in self._overrides:
            return self._overrides["TESLA_CLIENT_SECRET"] or None
        try:
            return _decouple_config("TESLA_CLIENT_SECRET", default=None)  # type: ignore[return-value]
        except UndefinedValueError:
            return None

    @property
    def tesla_private_key_path(self) -> Optional[str]:
        """Return Tesla private key file path, or None."""
        if "TESLA_PRIVATE_KEY_PATH" in self._overrides:
            return self._overrides["TESLA_PRIVATE_KEY_PATH"] or None
        return _decouple_config("TESLA_PRIVATE_KEY_PATH", default=None)

    @property
    def tesla_redirect_uri(self) -> str:
        """Return Tesla OAuth redirect URI."""
        if "TESLA_REDIRECT_URI" in self._overrides:
            return self._overrides["TESLA_REDIRECT_URI"]
        return _decouple_config("TESLA_REDIRECT_URI", default="", cast=str)  # type: ignore[return-value]

    @property
    def tesla_region(self) -> str:
        """Return Tesla API region."""
        if "TESLA_REGION" in self._overrides:
            return self._overrides["TESLA_REGION"]
        return _decouple_config("TESLA_REGION", default="na", cast=str)  # type: ignore[return-value]

    @property
    def tesla_vehicle_command_proxy_url(self) -> str | None:
        """Return the vehicle-command proxy URL, or None if not configured."""
        if "TESLA_VEHICLE_COMMAND_PROXY_URL" in self._overrides:
            return self._overrides["TESLA_VEHICLE_COMMAND_PROXY_URL"] or None
        try:
            return _decouple_config("TESLA_VEHICLE_COMMAND_PROXY_URL", default=None)
        except UndefinedValueError:
            return None

    @property
    def tesla_vehicle_id(self) -> Optional[str]:
        """Return Tesla vehicle ID, or None if not configured."""
        if "TESLA_VEHICLE_ID" in self._overrides:
            return self._overrides["TESLA_VEHICLE_ID"] or None
        try:
            return _decouple_config("TESLA_VEHICLE_ID", default=None)
        except UndefinedValueError:
            return None

    @property
    def tesla_home_lat(self) -> Optional[float]:
        """Return Tesla home latitude, or None if not configured."""
        if "TESLA_HOME_LAT" in self._overrides:
            val = self._overrides["TESLA_HOME_LAT"]
            if not val:
                return None
            try:
                return float(val)
            except ValueError:
                return None
        try:
            val = _decouple_config("TESLA_HOME_LAT", default=None)
            if val is None:
                return None
            return float(val)
        except (UndefinedValueError, ValueError):
            return None

    @property
    def tesla_home_lon(self) -> Optional[float]:
        """Return Tesla home longitude, or None if not configured."""
        if "TESLA_HOME_LON" in self._overrides:
            val = self._overrides["TESLA_HOME_LON"]
            if not val:
                return None
            try:
                return float(val)
            except ValueError:
                return None
        try:
            val = _decouple_config("TESLA_HOME_LON", default=None)
            if val is None:
                return None
            return float(val)
        except (UndefinedValueError, ValueError):
            return None

    @property
    def mqtt_host(self) -> str:
        """Return MQTT broker hostname (default: localhost)."""
        if "MQTT_HOST" in self._overrides:
            return self._overrides["MQTT_HOST"]
        return _decouple_config("MQTT_HOST", default="localhost", cast=str)  # type: ignore[return-value]

    @property
    def mqtt_port(self) -> int:
        """Return MQTT broker TCP port (default: 1883)."""
        return self._get_int("MQTT_PORT", default=1883)

    @property
    def mqtt_topic_base(self) -> str:
        """Return base MQTT topic prefix for fleet-telemetry messages.

        Returns:
            Topic base string (e.g. ``"tesla/telemetry"``).
        """
        if "MQTT_TOPIC_BASE" in self._overrides:
            return self._overrides["MQTT_TOPIC_BASE"]
        return _decouple_config("MQTT_TOPIC_BASE", default="tesla/telemetry", cast=str)  # type: ignore[return-value]

    @property
    def tesla_telemetry_ca_file(self) -> str | None:
        """Return path to CA PEM file for fleet-telemetry TLS, or None."""
        if "TESLA_TELEMETRY_CA_FILE" in self._overrides:
            val = self._overrides["TESLA_TELEMETRY_CA_FILE"]
            return val if val else None
        return _decouple_config("TESLA_TELEMETRY_CA_FILE", default=None)

    @property
    def tesla_telemetry_chargestate_interval(self) -> int:
        """Return min interval for ChargeState telemetry (seconds, default 15)."""
        if "TESLA_TELEMETRY_CHARGESTATE_INTERVAL_SEC" in self._overrides:
            return int(self._overrides["TESLA_TELEMETRY_CHARGESTATE_INTERVAL_SEC"])
        raw = _decouple_config("TESLA_TELEMETRY_CHARGESTATE_INTERVAL_SEC", default=None)
        if raw is not None:
            return int(raw)
        return 15

    @property
    def tesla_telemetry_location_interval(self) -> int:
        """Return min interval for Location telemetry (seconds, default 120)."""
        if "TESLA_TELEMETRY_LOCATION_INTERVAL_SEC" in self._overrides:
            return int(self._overrides["TESLA_TELEMETRY_LOCATION_INTERVAL_SEC"])
        raw = _decouple_config("TESLA_TELEMETRY_LOCATION_INTERVAL_SEC", default=None)
        if raw is not None:
            return int(raw)
        return 120

    @property
    def tesla_telemetry_chargeamps_interval(self) -> int:
        """Return min interval for ChargeAmps telemetry (seconds, default 15)."""
        if "TESLA_TELEMETRY_CHARGEAMPS_INTERVAL_SEC" in self._overrides:
            return int(self._overrides["TESLA_TELEMETRY_CHARGEAMPS_INTERVAL_SEC"])
        raw = _decouple_config("TESLA_TELEMETRY_CHARGEAMPS_INTERVAL_SEC", default=None)
        if raw is not None:
            return int(raw)
        return 15

    @property
    def tesla_telemetry_detailedchargestate_interval(self) -> int:
        """Return min interval for DetailedChargeState telemetry (seconds, default 15)."""
        if "TESLA_TELEMETRY_DETAILEDCHARGESTATE_INTERVAL_SEC" in self._overrides:
            return int(self._overrides["TESLA_TELEMETRY_DETAILEDCHARGESTATE_INTERVAL_SEC"])
        raw = _decouple_config("TESLA_TELEMETRY_DETAILEDCHARGESTATE_INTERVAL_SEC", default=None)
        if raw is not None:
            return int(raw)
        return 15

    @property
    def public_url(self) -> str:
        """Return the public URL the app is served on, or a sensible default."""
        return _decouple_config("PUBLIC_URL", default="http://localhost:8000", cast=str)  # type: ignore[return-value]

    @property
    def load_plug_controller(self) -> str:
        """Return plug controller type (real or stub)."""
        if "LOAD_PLUG_CONTROLLER" in self._overrides:
            return self._overrides["LOAD_PLUG_CONTROLLER"].lower()
        return _decouple_config("LOAD_PLUG_CONTROLLER", default="stub", cast=str).lower()

    @property
    def load_tesla_controller(self) -> str:
        """Return Tesla controller type (real or stub)."""
        if "LOAD_TESLA_CONTROLLER" in self._overrides:
            return self._overrides["LOAD_TESLA_CONTROLLER"].lower()
        return _decouple_config("LOAD_TESLA_CONTROLLER", default="stub", cast=str).lower()

    @property
    def vocolinc_username(self) -> str:
        """Return VOCOlinc username, or empty string."""
        if "VOCOLINC_USERNAME" in self._overrides:
            return self._overrides["VOCOLINC_USERNAME"].strip()
        return _decouple_config("VOCOLINC_USERNAME", default="", cast=str).strip()  # type: ignore[return-value]

    @property
    def vocolinc_password(self) -> str:
        """Return VOCOlinc password, or empty string."""
        if "VOCOLINC_PASSWORD" in self._overrides:
            return self._overrides["VOCOLINC_PASSWORD"].strip()
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
        return device_config.get_all_plugs()

    def get_all(self) -> dict[str, Any]:
        """Return full devices.json contents as a dict."""
        return device_config.get_all()

    def reload(self) -> None:
        """Clear all cached configuration."""
        device_config.reload()


# Module-level singleton for convenience (backward compatible)
_config = Config()


def get_timezone() -> str:
    """Return configured timezone — backward compatible alias."""
    return _config.timezone


def reload() -> None:
    """Reload configuration — backward compatible alias."""
    _config.reload()


# === Config file hot-reload support ===


@dataclass
class ConfigChanges:
    """Summary of detected config file changes."""

    env_changed: list[str] | None = None
    devices_changed: bool = False


RESTART_REQUIRED_KEYS = frozenset({
    "TESLA_CLIENT_ID", "TESLA_CLIENT_SECRET", "TESLA_VEHICLE_ID",
    "TESLA_PRIVATE_KEY_PATH", "MQTT_HOST", "MQTT_PORT", "MQTT_TOPIC_BASE",
    "LOAD_PLUG_CONTROLLER", "LOAD_TESLA_CONTROLLER",
    "VOCOLINC_USERNAME", "VOCOLINC_PASSWORD",
    "VUE_USERNAME", "VUE_PASSWORD",
    "LOAD_MANAGE_INTERVAL_SECS",
})


def check_restart_required(changed_keys: list[str]) -> list[str]:
    """Return subset of changed_keys that require a restart."""
    return [k for k in changed_keys if k in RESTART_REQUIRED_KEYS]


def _parse_env_file(path: Path) -> dict[str, str]:
    """Parse a .env file into a dict of key=value pairs."""
    data: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return data
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip()
        if len(v) >= 2 and ((v[0] == "'" and v[-1] == "'") or (v[0] == '"' and v[-1] == '"')):
            v = v[1:-1]
        data[k] = v
    return data


def _update_decouple_repository(new_data: dict[str, str]) -> None:
    """Push updated values into decouple's RepositoryEnv.data dict."""
    _decouple_config._ensure_loaded()  # type: ignore[attr-defined]
    repo = _decouple_config.config.repository  # type: ignore[attr-defined]
    if hasattr(repo, "data"):
        repo.data.update(new_data)


def reload_dotenv(path: Path | None = None) -> list[str]:
    """Re-read .env file into os.environ and decouple cache.

    Returns list of keys that changed (new or modified).
    Does NOT remove keys that were deleted from the file.
    """
    if path is None:
        path = Path(".env")
    new_data = _parse_env_file(path)
    if not new_data:
        return []

    changed: list[str] = []
    for key, value in new_data.items():
        old = os.environ.get(key)
        if old != value:
            changed.append(key)
            os.environ[key] = value

    if changed:
        _update_decouple_repository(new_data)

    return changed


class ConfigWatcher:
    """Tracks file mtimes and triggers reload when files change.

    Designed to be called from run_cycle() — no separate thread.
    On construction, records current mtimes so the first check() does not
    report changes for files that already exist.
    """

    def __init__(
        self,
        env_path: Path | None = None,
        devices_path: Path | None = None,
    ) -> None:
        self._env_path = env_path or Path(".env")
        self._devices_path = devices_path or Path("devices.json")
        self._env_mtime = self._safe_mtime(self._env_path)
        self._devices_mtime = self._safe_mtime(self._devices_path)

    @staticmethod
    def _safe_mtime(path: Path) -> float:
        """Return file mtime, or 0.0 if file doesn't exist."""
        try:
            return path.stat().st_mtime if path.exists() else 0.0
        except OSError:
            return 0.0

    def check(self) -> ConfigChanges:
        """Check both files for changes. Returns summary of what changed."""
        changes = ConfigChanges()

        if self._env_path.exists():
            try:
                new_mtime = self._env_path.stat().st_mtime
                if new_mtime > self._env_mtime:
                    changed_keys = reload_dotenv(self._env_path)
                    self._env_mtime = new_mtime
                    if changed_keys:
                        changes.env_changed = changed_keys
            except OSError:
                pass

        if self._devices_path.exists():
            try:
                new_mtime = self._devices_path.stat().st_mtime
                if new_mtime > self._devices_mtime:
                    device_config.reload()
                    self._devices_mtime = new_mtime
                    changes.devices_changed = True
            except OSError:
                pass

        return changes
