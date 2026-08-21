"""Centralized application configuration.

Provides typed accessor methods for all settings, reading from environment
variables (via the standard library: instance overrides, ``os.environ``,
then a lazily-parsed ``.env`` file) and devices.json in a unified interface.
Callers never need to know which source a setting comes from.

Lookup precedence (highest first):
    1. ``Config(overrides={...})`` instance overrides
    2. ``os.environ`` (also the target of ``Config.set()``/``clear()``)
    3. Lazily-parsed ``.env`` file

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

import device_config


logger = logging.getLogger(__name__)


class UndefinedValueError(Exception):
    """Raised when a config key is missing and no default was provided."""


class _Undefined:
    """Sentinel type for missing ``default`` arguments to ``Config.get()``."""


_UNDEFINED = _Undefined()

# Lazily-parsed .env cache. ``None`` means "not parsed yet"; ``{}`` means
# "parsed and empty" (so ``clear_all()`` prevents .env re-pollution).
_env_data: dict[str, str] | None = None

# Environment variables owned by this application. ``clear_all()`` removes
# only these keys from ``os.environ`` so unrelated environment variables
# (PATH, HOME, test-runner vars, etc.) are left intact.
_APP_ENV_KEY_PREFIXES = (
    "LOAD_", "LOG_", "MQTT_", "TELEGRAM_", "TESLA_", "VOCOLINC_", "VUE_",
)
_APP_ENV_KEY_NAMES = frozenset({
    "DEBUG", "MOCK", "MOCK_ERROR", "PUBLIC_URL", "TIMEZONE",
})


def _is_app_key(key: str) -> bool:
    """Return True when *key* is an environment variable owned by this app."""
    if key in _APP_ENV_KEY_NAMES:
        return True
    return any(key.startswith(prefix) for prefix in _APP_ENV_KEY_PREFIXES)


def _strtobool(value: str) -> bool:
    """Convert a string representation of truth to a bool."""
    return value.lower() in {"y", "yes", "t", "true", "on", "1"}


def _get_env_data() -> dict[str, str]:
    """Parse the .env file lazily (once) and return the key→value dict.

    Returns:
        Dict of key→value pairs from the .env file (empty when missing).
    """
    global _env_data
    if _env_data is None:
        _env_data = _parse_env_file(Path(".env"))
    return _env_data


def _lookup(key: str, default: Any = _UNDEFINED, cast: Any = None) -> Any:
    """Module-level lookup: os.environ → .env file → default.

    This is the shared lookup used by :class:`Config` (instance overrides
    are handled by the caller). Kept as a module-level function so tests
    can patch a single seam to control config reads.

    Args:
        key: Config key to look up.
        default: Value returned when the key is missing from every source.
        cast: Optional callable applied to the value (``bool`` is handled
            specially via :func:`_strtobool`).

    Returns:
        The string value, or *default* when not found.

    Raises:
        UndefinedValueError: If the key is missing and no default given.
    """
    value: Any
    if key in os.environ:
        value = os.environ[key]
    else:
        env_data = _get_env_data()
        value = env_data[key] if key in env_data else _UNDEFINED
    if isinstance(value, _Undefined):
        if isinstance(default, _Undefined):
            raise UndefinedValueError(
                f"{key} not found. "
                "Declare it as envvar or define a default value."
            )
        value = default
    if cast is not None and value is not None:
        if cast is bool:
            return _strtobool(str(value))
        return cast(value)
    return value


class Config:
    # Too many public methods (47/20): Config is a typed facade over the
    # env lookup chain — each accessor is a thin typed getter for one
    # setting (or a small derived property).  Splitting it would scatter
    # related settings across classes without reducing real complexity.
    # pylint: disable=too-many-public-methods
    """Unified configuration accessor for all Solara settings.

    Reads from environment variables first, falls back to devices.json
    where applicable. Derived properties combine multiple sources.

    Args:
        overrides: Optional dict of key→value pairs that take precedence
            over os.environ and the .env file. Used by tests to inject
            config without touching the shared environment.
    """

    def __init__(self, overrides: dict[str, str] | None = None) -> None:
        self._overrides = overrides or {}

    # ── Raw lookup chain ────────────────────────────────────────────────

    def get(
        self,
        key: str,
        default: Any = _UNDEFINED,
        cast: Any = None,
    ) -> Any:
        """Return the value for *key* or *default* if undefined.

        Args:
            key: Config key to look up.
            default: Value returned when the key is missing. When omitted,
                :class:`UndefinedValueError` is raised for missing keys.
            cast: Optional callable applied to the value (``bool`` is handled
                specially via :func:`_strtobool`).

        Returns:
            The raw (optionally cast) value.

        Raises:
            UndefinedValueError: If the key is missing and no default given.
        """
        if key in self._overrides:
            value = self._overrides[key]
        else:
            value = _lookup(key, default)
        if cast is not None and value is not None:
            if cast is bool:
                return _strtobool(str(value))
            return cast(value)
        return value

    def set(self, key: str, value: str | None) -> None:
        """Set a config value in ``os.environ`` (top precedence).

        Args:
            key: Config key to set.
            value: String value; ``None`` is stored as an empty string so
                lookups see the key as present-but-unset.
        """
        os.environ[key] = "" if value is None else str(value)

    def clear(self, key: str) -> None:
        """Remove a config key from ``os.environ`` and the .env cache."""
        os.environ.pop(key, None)
        _get_env_data().pop(key, None)

    def clear_all(self) -> None:
        """Remove all app-owned config keys from ``os.environ``.

        Only keys owned by this application (see ``_APP_ENV_KEY_PREFIXES``
        and ``_APP_ENV_KEY_NAMES``) are removed; unrelated environment
        variables are left intact. The lazily-parsed .env cache is then
        marked parsed-empty so subsequent lookups cannot be re-polluted by
        the .env file.
        """
        global _env_data
        for key in list(os.environ):
            if _is_app_key(key):
                os.environ.pop(key, None)
        _env_data = {}

    def get_all(self) -> dict[str, str]:
        """Return all config values from .env and os.environ.

        os.environ takes precedence, matching the lookup order.

        Returns:
            Dict of all config key-value pairs as strings.
        """
        merged = dict(_get_env_data())
        merged.update(os.environ)
        return merged

    # ── Typed helpers ───────────────────────────────────────────────────

    def _get(self, key: str, default: str | None = None) -> str | None:
        """Return the string value for *key*, or *default* when missing."""
        if key in self._overrides:
            return self._overrides[key]
        try:
            return _lookup(key, default)
        except UndefinedValueError:
            return default

    def _get_bool(self, key: str, default: str = "False") -> bool:
        """Return boolean config value, checking each source in order."""
        if key in self._overrides:
            return _strtobool(str(self._overrides[key]))
        try:
            raw = _lookup(key, default)
        except UndefinedValueError:
            raw = default
        return _strtobool(str(raw))

    def _get_int(self, key: str, default: int = 0) -> int:
        """Return integer config value, checking each source in order."""
        if key in self._overrides:
            return int(self._overrides[key])
        raw = _lookup(key, None)
        if raw is None:
            return default
        return int(raw)

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
        raw = self._get("LOAD_MANAGE_ENABLED")
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
        raw = self._get("LOAD_TARGET_WH")
        if raw is not None:
            return int(raw)
        return device_config.get_target_wh()

    @property
    def load_manage_interval_secs(self) -> int:
        """Return seconds between load management cycles."""
        return self._get_int("LOAD_MANAGE_INTERVAL_SECS", default=30)

    @property
    def load_manage_api_key(self) -> str:
        """Return API key for manual load management endpoint."""
        return self._get("LOAD_MANAGE_API_KEY", "") or ""

    @property
    def load_nbc_device(self) -> str:
        """Return NBC device name for load management."""
        env_val = self._get("LOAD_NBC_DEVICE")
        if env_val:
            return env_val
        return device_config.get_smartmeter_device()

    @property
    def debug(self) -> bool:
        """Return True when DEBUG mode is enabled."""
        return self._get_bool("DEBUG")

    @property
    def log_file(self) -> str | None:
        """Return log file path, or None to disable file logging."""
        return self._get("LOG_FILE", default=None)

    @property
    def log_max_bytes(self) -> int:
        """Return max log file size in bytes before rotation."""
        return self._get_int("LOG_MAX_BYTES", default=10_485_760)

    @property
    def log_backup_count(self) -> int:
        """Return number of rotated log files to keep."""
        return self._get_int("LOG_BACKUP_COUNT", default=5)

    @property
    def dry_run(self) -> bool:
        """Return True when load management is in dry-run mode."""
        return self._get_bool("LOAD_MANAGE_DRY_RUN")

    @property
    def vue_username(self) -> Optional[str]:
        """Return Emporia VUE username, or None if not configured."""
        val = self._get("VUE_USERNAME")
        return val if val else None

    @property
    def vue_password(self) -> Optional[str]:
        """Return Emporia VUE password, or None if not configured."""
        val = self._get("VUE_PASSWORD")
        return val if val else None

    @property
    def tesla_client_id(self) -> Optional[str]:
        """Return Tesla Fleet API client ID, or None if not configured."""
        return self._get("TESLA_CLIENT_ID") or None

    @property
    def tesla_client_secret(self) -> Optional[str]:
        """Return Tesla Fleet API client secret, or None if not configured."""
        return self._get("TESLA_CLIENT_SECRET") or None

    @property
    def tesla_private_key_path(self) -> Optional[str]:
        """Return Tesla private key file path, or None."""
        return self._get("TESLA_PRIVATE_KEY_PATH") or None

    @property
    def tesla_redirect_uri(self) -> str:
        """Return Tesla OAuth redirect URI."""
        return self._get("TESLA_REDIRECT_URI", "") or ""

    @property
    def tesla_region(self) -> str:
        """Return Tesla API region."""
        return self._get("TESLA_REGION", "na") or "na"

    @property
    def tesla_vehicle_command_proxy_url(self) -> str | None:
        """Return the vehicle-command proxy URL, or None if not configured."""
        return self._get("TESLA_VEHICLE_COMMAND_PROXY_URL") or None

    @property
    def tesla_vehicle_id(self) -> Optional[str]:
        """Return Tesla vehicle ID, or None if not configured."""
        return self._get("TESLA_VEHICLE_ID") or None

    @property
    def tesla_home_lat(self) -> Optional[float]:
        """Return Tesla home latitude, or None if not configured."""
        val = self._get("TESLA_HOME_LAT")
        if val is None or not val:
            return None
        try:
            return float(val)
        except ValueError:
            return None

    @property
    def tesla_home_lon(self) -> Optional[float]:
        """Return Tesla home longitude, or None if not configured."""
        val = self._get("TESLA_HOME_LON")
        if val is None or not val:
            return None
        try:
            return float(val)
        except ValueError:
            return None

    @property
    def mqtt_host(self) -> str:
        """Return MQTT broker hostname (default: localhost)."""
        return self._get("MQTT_HOST", "localhost") or "localhost"

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
        return self._get("MQTT_TOPIC_BASE", "tesla/telemetry") or "tesla/telemetry"

    @property
    def tesla_telemetry_ca_file(self) -> str | None:
        """Return path to CA PEM file for fleet-telemetry TLS, or None."""
        val = self._get("TESLA_TELEMETRY_CA_FILE")
        return val if val else None

    @property
    def tesla_telemetry_chargestate_interval(self) -> int:
        """Return min interval for ChargeState telemetry (seconds, default 15)."""
        return self._get_int("TESLA_TELEMETRY_CHARGESTATE_INTERVAL_SEC", default=15)

    @property
    def tesla_telemetry_location_interval(self) -> int:
        """Return min interval for Location telemetry (seconds, default 120)."""
        return self._get_int("TESLA_TELEMETRY_LOCATION_INTERVAL_SEC", default=120)

    @property
    def tesla_telemetry_chargeamps_interval(self) -> int:
        """Return min interval for ChargeAmps telemetry (seconds, default 15)."""
        return self._get_int("TESLA_TELEMETRY_CHARGEAMPS_INTERVAL_SEC", default=15)

    @property
    def tesla_telemetry_detailedchargestate_interval(self) -> int:
        """Return min interval for DetailedChargeState telemetry (seconds, default 15)."""
        return self._get_int("TESLA_TELEMETRY_DETAILEDCHARGESTATE_INTERVAL_SEC", default=15)

    @property
    def public_url(self) -> str:
        """Return the public URL the app is served on, or a sensible default."""
        return self._get("PUBLIC_URL", "http://localhost:8000") or "http://localhost:8000"

    @property
    def load_plug_controller(self) -> str:
        """Return plug controller type (real or stub)."""
        return (self._get("LOAD_PLUG_CONTROLLER", "stub") or "stub").lower()

    @property
    def load_tesla_controller(self) -> str:
        """Return Tesla controller type (real or stub)."""
        return (self._get("LOAD_TESLA_CONTROLLER", "stub") or "stub").lower()

    @property
    def vocolinc_username(self) -> str:
        """Return VOCOlinc username, or empty string."""
        return (self._get("VOCOLINC_USERNAME", "") or "").strip()

    @property
    def vocolinc_password(self) -> str:
        """Return VOCOlinc password, or empty string."""
        return (self._get("VOCOLINC_PASSWORD", "") or "").strip()

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
    """Parse a .env file into a dict of key=value pairs.

    Supports full-line comments, blank lines, optional surrounding
    whitespace, single/double-quoted values, and inline comments on
    unquoted values (``KEY=value # note`` → ``value``).  A ``#`` inside
    a quoted value is kept (``KEY="a#b"`` → ``a#b``).
    """
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
        else:
            # Unquoted value: strip an inline comment ("KEY=value # note").
            # Re-check for quotes afterwards so "KEY=\"x\" # note" → "x".
            v = v.split("#", 1)[0].rstrip()
            if len(v) >= 2 and ((v[0] == "'" and v[-1] == "'") or (v[0] == '"' and v[-1] == '"')):
                v = v[1:-1]
        data[k] = v
    return data


def _reset_env_cache() -> None:
    """Reset the lazy .env cache so the next lookup re-reads the file."""
    global _env_data
    _env_data = None


def reload_dotenv(path: Path | None = None) -> list[str]:
    """Re-read .env file into os.environ and the lazy .env cache.

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
        _reset_env_cache()

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
            except OSError as e:
                logger.warning(
                    "Hot-reload check failed for %s: %s",
                    self._env_path, e, exc_info=True,
                )

        if self._devices_path.exists():
            try:
                new_mtime = self._devices_path.stat().st_mtime
                if new_mtime > self._devices_mtime:
                    device_config.reload()
                    self._devices_mtime = new_mtime
                    changes.devices_changed = True
            except OSError as e:
                logger.warning(
                    "Hot-reload check failed for %s: %s",
                    self._devices_path, e, exc_info=True,
                )

        return changes
