"""
All controller implementations (stub + real), token/pairing persistence,
and CLI helpers.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import time as _time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast, Literal

import sys
from unittest.mock import MagicMock

import aiohttp
from aiohomekit.controller.abstract import AbstractPairing

import config as _cfg_mod
from config import Config


from load_models import (
    AbstractPlugController,
    AbstractTeslaController,
    FleetTelemetryProvisionConfig,
    PlugAction,
    PlugConfig,
    TeslaAuthError,
    TeslaConfig,
    TeslaState,
)


logger = logging.getLogger(__name__)

# Exponential backoff for Tesla init when vehicle is offline/asleep.
CAR_OFFLINE_BACKOFF_SECS: float = 30.0
"""Initial backoff in seconds after a Tesla init failure (e.g. VehicleOffline)."""
CAR_OFFLINE_BACKOFF_MAX: float = 900.0
"""Maximum backoff in seconds (15 minutes) before retrying Tesla init."""


# Default path for pairing persistence
PAIRINGS_FILE = Path(".homekit-pairings.json")


def _is_auth_error(exc: BaseException) -> bool:
    """Check if an exception indicates a Tesla auth/credential problem.

    Args:
        exc: The exception to check.

    Returns:
        True if the error message contains auth-related keywords.
    """
    error_str = str(exc).lower()
    keywords = ["login_required", "refresh_token", "unauthorized", "authentication failed"]
    return any(kw in error_str for kw in keywords)

# Default path for Tesla OAuth token persistence
TESLA_TOKENS_FILE = Path(".tesla-tokens.json")


class PlugController(AbstractPlugController):
    """In-memory stub for smart plug control.

    Stores on/off state per plug and logs all actions for test verification.
    All methods are no-ops that return success with in-memory state tracking.
    """

    def __init__(self, plugs: dict[str, PlugConfig]) -> None:
        self.plugs = plugs
        self._state: dict[str, bool] = {name: False for name in plugs}
        self.action_log: list[PlugAction] = []

    async def get_state(self, name: str) -> bool | None:
        """Return stored on/off state for the named plug."""
        if name not in self._state:
            logger.warning("Unknown plug %s", name)
            return None
        return self._state[name]

    async def set_state(self, name: str, on: bool) -> bool:
        """Set plug on/off state and log the action."""
        if name not in self._state:
            logger.warning("Unknown plug %s", name)
            return False
        logger.info("PlugController.set_state(%s, %s)", name, on)
        self._state[name] = on
        self.action_log.append(PlugAction(name=name, on=on, timestamp=datetime.now(timezone.utc)))
        return True


class TeslaController(AbstractTeslaController):
    """In-memory stub for Tesla vehicle control.

    Returns configurable defaults and supports test scenario setup via
    set_mock_state(). All methods are no-ops that log the action.
    """

    def __init__(self, tesla_config: TeslaConfig) -> None:
        self.config = tesla_config
        self.last_error: str | None = None
        self._state = TeslaState(
            is_charging=False,
            current_amps=None,
            plugged_in=False,
            at_home=False,
        )

    def set_mock_state(self, state: TeslaState) -> None:
        """Replace internal state for test scenarios."""
        self._state = state

    async def authenticate(self) -> None:
        """No-op authentication stub."""
        logger.info("TeslaController.authenticate() [stub]")

    async def is_at_home(self) -> bool:
        """Return stored at_home flag."""
        return self._state.at_home

    async def is_plugged_in(self) -> bool:
        """Return stored plugged_in flag."""
        return self._state.plugged_in

    async def start_charging(self) -> bool:
        """Set charging state to True and log the action."""
        logger.info("TeslaController.start_charging() [stub]")
        self._state.is_charging = True
        if self._state.current_amps is None:
            self._state.current_amps = self.config.charge_amps_min
        return True

    async def stop_charging(self) -> bool:
        """Set charging state to False and log the action."""
        logger.info("TeslaController.stop_charging() [stub]")
        self._state.is_charging = False
        self._state.current_amps = None
        return True

    async def set_charge_amps(self, amps: int) -> bool:
        """Set charge amps within configured range and log the action."""
        clamped = max(
            self.config.charge_amps_min, min(self.config.charge_amps_max, amps)
        )
        # Belt-and-suspenders: hard absolute max regardless of config.
        HARD_MAX_AMPS = 48
        clamped = min(clamped, HARD_MAX_AMPS)
        logger.info("TeslaController.set_charge_amps(%d) [stub]", clamped)
        self._state.current_amps = clamped
        if not self._state.is_charging:
            self._state.is_charging = True
        return True

    async def get_charging_state(self) -> TeslaState | None:
        """Return a copy of the current internal state."""
        return TeslaState(
            is_charging=self._state.is_charging,
            current_amps=self._state.current_amps,
            plugged_in=self._state.plugged_in,
            at_home=self._state.at_home,
        )


def _haversine_distance(
    lat1: float, lon1: float, lat2: float, lon2: float
) -> float:
    """Calculate the great-circle distance between two GPS points.

    Args:
        lat1: Latitude of point 1 in degrees.
        lon1: Longitude of point 1 in degrees.
        lat2: Latitude of point 2 in degrees.
        lon2: Longitude of point 2 in degrees.

    Returns:
        Distance in meters.
    """
    earth_radius_m = 6_371_000  # Earth radius in meters
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)

    a = (
        math.sin(delta_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
    )
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

    return earth_radius_m * c


def load_tesla_tokens(tokens_path: Path = TESLA_TOKENS_FILE) -> dict[str, Any] | None:
    """Load persisted Tesla OAuth tokens from JSON file.

    Args:
        tokens_path: Path to the token file.

    Returns:
        Dict with refresh_token, access_token, expires keys, or None if unavailable.
    """
    if not tokens_path.exists():
        return None
    try:
        with open(tokens_path, encoding="utf-8") as f:
            data = json.load(f)
        required = ("refresh_token", "access_token", "expires")
        if all(key in data for key in required):
            return data
        logger.warning("Tesla token file missing required fields, ignoring")
        return None
    except (json.JSONDecodeError, OSError) as e:
        logger.error("Failed to load Tesla tokens file: %s", e)
        return None


def save_tesla_tokens(
    refresh_token: str,
    access_token: str,
    expires: int,
    tokens_path: Path = TESLA_TOKENS_FILE,
) -> None:
    """Persist Tesla OAuth tokens to JSON file.

    Args:
        refresh_token: Long-lived refresh token.
        access_token: Short-lived access token.
        expires: Unix timestamp when the access token expires.
        tokens_path: Path to write the token file.
    """
    try:
        data = {
            "refresh_token": refresh_token,
            "access_token": access_token,
            "expires": expires,
        }
        with open(tokens_path, "w", encoding="utf-8") as f:
            json.dump(data, f)
        logger.info("Tesla tokens persisted to %s", tokens_path)
    except OSError as e:
        logger.error("Failed to save Tesla tokens: %s", e)


def remove_tesla_tokens(tokens_path: Path = TESLA_TOKENS_FILE) -> None:
    """Remove persisted Tesla OAuth token file.

    Args:
        tokens_path: Path to the token file.
    """
    try:
        if tokens_path.exists():
            tokens_path.unlink()
            logger.info("Tesla tokens removed from %s", tokens_path)
    except OSError as e:
        logger.error("Failed to remove Tesla tokens: %s", e)


# === Real Tesla Controller Implementation ===


class RealTeslaController(AbstractTeslaController):
    """Controls Tesla vehicle charging via tesla-fleet-api.

    Uses OAuth to authenticate, then calls the Fleet API for vehicle data
    and commands. Implements haversine distance check for home detection.
    All API calls are wrapped in try/except for resilience.
    """

    def __init__(self, tesla_config: TeslaConfig, config: Config | None = None) -> None:
        self.config = tesla_config
        self._cfg = config if config is not None else _cfg_mod._config
        self.last_error: str | None = None
        self._session: aiohttp.ClientSession | None = None
        self._api: Any | None = None  # TeslaFleetOAuth instance
        self._vin: str = ""
        self._init_state: TeslaState | None = None
        self._last_init_attempt: float = 0.0
        """Monotonic time of the last Tesla init attempt (0 = never attempted)."""
        self._backoff_secs: float = 0.0
        """Current exponential backoff duration in seconds."""
        self._last_saved_tokens_at: float = 0.0
        """Monotonic time of the last save_tokens() call (0 = never saved)."""

    async def _get_session(self, ssl: bool = True) -> aiohttp.ClientSession:
        """Get or create the aiohttp session.

        Args:
            ssl: Whether to verify SSL certificates. Set to ``False`` when
                using a vehicle-command proxy with a hostname-mismatched
                certificate.

        NOTE: A cached ClientSession binds to the event loop active at creation
        time.  When ``asyncio.run()`` creates a fresh loop and closes it, the
        cached session becomes unusable even though ``session.closed`` is still
        ``False``.  This is why ``reset_session()`` must be called before each
        ``asyncio.run()`` invocation.
        """
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(ssl=ssl)
            self._session = aiohttp.ClientSession(connector=connector)
        return self._session

    async def _ensure_api(self) -> None:
        """Initialize the TeslaFleetOAuth API client if needed.

        Reloads persisted tokens from disk on each call so that tokens written
        by an external OAuth callback are picked up without restarting gunicorn.
        """
        from tesla_fleet_api import TeslaFleetOAuth

        session = await self._get_session(
            ssl=not bool(self.config.vehicle_command_proxy_url),
        )
        region = cast(Literal["na", "eu", "cn"], self._cfg.tesla_region)

        tokens = load_tesla_tokens()
        if tokens is not None:
            logger.info("Loaded persisted Tesla OAuth tokens")

        if self._api is None:
            self._api = TeslaFleetOAuth(
                session=session,
                region=region,
                client_id=self.config.client_id,
                client_secret=self.config.client_secret,
                redirect_uri=self.config.redirect_uri,
                access_token=tokens.get("access_token") if tokens else None,
                refresh_token=tokens.get("refresh_token") if tokens else None,
                expires=tokens.get("expires", 0) if tokens else 0,
            )
            if not self.config.private_key_path:
                logger.warning(
                    "TESLA_PRIVATE_KEY_PATH not set; vehicle commands (set_amps, "
                    "start/stop charging) will be unavailable"
                )
            elif not os.path.exists(self.config.private_key_path):
                logger.error(
                    "Tesla private key file not found at '%s'; vehicle commands will "
                    "be unavailable until the key is placed at that path",
                    self.config.private_key_path,
                )
            else:
                await self._api.get_private_key(self.config.private_key_path)
                logger.debug("Loaded Tesla private key from %s", self.config.private_key_path)

            # Override Tesla API server URL if a vehicle-command proxy is
            # configured.  Setting ``self._api.server`` after construction
            # overrides the region-based URL for all subsequent API calls.
            if self.config.vehicle_command_proxy_url:
                self._api.server = self.config.vehicle_command_proxy_url
                logger.info(
                    "Using vehicle-command proxy for Tesla API: %s",
                    self.config.vehicle_command_proxy_url,
                )
        else:
            # API already exists — update the session in case the previous one
            # was closed between calls, then refresh tokens from disk.
            self._api.session = session
            if tokens is not None:
                # pylint: disable=protected-access
                # TeslaFleetOAuth exposes no public API to update tokens;
                # we must mutate internals to pick up externally-saved tokens.
                self._api._access_token = tokens.get("access_token")
                self._api.refresh_token = tokens.get("refresh_token")
                self._api.expires = tokens.get("expires", 0)

    async def authenticate(self) -> None:
        """Load cached token or refresh to obtain a valid access token.

        Reads persisted tokens from `.tesla-tokens.json`. If the access token
        is expired or rejected by Tesla's servers, attempts to refresh it and
        saves the updated tokens.
        Raises RuntimeError if no tokens are available or refresh fails.
        """
        await self._ensure_api()
        assert self._api is not None

        # Use access_token() which makes a real API call and refreshes when
        # expired. This catches server-side token revocation that the local
        # expiry check in check_access_token() misses.
        try:
            await self._api.access_token()
        except (ValueError, Exception) as e:
            if isinstance(e, (KeyboardInterrupt, SystemExit)):
                raise
            logger.warning("Tesla access token refresh failed: %s", e)
            raise RuntimeError(
                "Tesla OAuth not configured. Visit /api/v1/tesla/auth/initiate "
                "to perform initial authentication."
            ) from e

        # Save whatever tokens we have (refreshed or original)
        try:
            # pylint: disable=protected-access
            # TeslaFleetOAuth exposes no public getter for access_token.
            save_tesla_tokens(
                refresh_token=self._api.refresh_token,  # type: ignore[attr-defined]
                access_token=self._api._access_token,   # type: ignore[attr-defined]
                expires=self._api.expires,              # type: ignore[attr-defined]
            )
        except Exception as e:
            logger.warning("Failed to save Tesla tokens: %s", e)

        self._vin = self.config.vehicle_id
        logger.info("Tesla authenticated successfully (cached/refreshed token), vin=%s", self._vin)

    def get_login_url(self, state: str = "solara-login") -> str:
        """Generate the Tesla OAuth authorization URL.

        Args:
            state: OAuth state parameter for CSRF protection.

        Returns:
            URL to open in browser for user authorization.
        """
        from tesla_fleet_api.const import Scope

        region = self._cfg.tesla_region
        domain = "auth.tesla.cn" if region == "cn" else "auth.tesla.com"
        scope_str = "+".join(
            [
                Scope.OPENID.value,
                Scope.EMAIL.value,
                Scope.PROFILE.value,
                Scope.OFFLINE_ACCESS.value,
                Scope.VEHICLE_DEVICE_DATA.value,
                Scope.VEHICLE_CHARGING_CMDS.value,
                Scope.VEHICLE_LOCATION.value,
            ]
        )
        return (
            f"https://{domain}/oauth2/v3/authorize"
            f"?response_type=code"
            f"&client_id={self.config.client_id}"
            f"&redirect_uri={self.config.redirect_uri}"
            f"&scope={scope_str}"
            f"&state={state}"
        )

    async def exchange_code(self, code: str) -> None:
        """Exchange an OAuth authorization code for tokens.

        Args:
            code: Authorization code from Tesla's callback URL.
        """
        await self._ensure_api()
        assert self._api is not None
        try:
            await self._api.get_refresh_token(code)
            logger.info("Tesla OAuth token exchange successful")
        except BaseException as e:
            if isinstance(e, (KeyboardInterrupt, SystemExit)):
                raise
            logger.error("Tesla OAuth token exchange failed: %s", e)
            raise

    def save_tokens(self) -> None:
        """Persist current tokens to disk after a successful refresh."""
        if self._api is None:
            return
        try:
            # pylint: disable=protected-access
            # TeslaFleetOAuth exposes no public getter for access_token.
            save_tesla_tokens(
                refresh_token=self._api.refresh_token,  # type: ignore[attr-defined]
                access_token=self._api._access_token,   # type: ignore[attr-defined]
                expires=self._api.expires,              # type: ignore[attr-defined]
            )
            self._last_saved_tokens_at = _time.monotonic()
        except BaseException as e:
            if isinstance(e, (KeyboardInterrupt, SystemExit)):
                raise
            logger.error("Failed to save Tesla tokens after refresh: %s", e)

    async def _get_vehicle(self):
        """Get the vehicle instance for our VIN."""
        await self._ensure_api()
        assert self._api is not None
        if self._api.has_private_key:
            return self._api.vehicles.specificSigned(self.config.vehicle_id)
        return self._api.vehicles.specific(self.config.vehicle_id)

    async def _fetch_vehicle_data(
        self, endpoints: list[str] | None = None
    ) -> dict[str, Any]:
        """Fetch vehicle data from the API.

        Args:
            endpoints: List of endpoints to fetch (e.g., ["drive_state", "charge_state"]).
                      If None, fetches all available data.

        Returns:
            Raw vehicle data dictionary.

        Raises:
            Exception: On API failure.
        """
        vehicle = await self._get_vehicle()
        return await vehicle.vehicle_data(endpoints=endpoints)

    async def is_at_home(self) -> bool:
        """Check vehicle GPS against home lat/lon (deprecated).

        Deprecated: REST-based status polling is no longer used.
        The load manager does not require GPS proximity checks.
        This method always returns False.

        Returns:
            Always False.
        """
        logger.warning(
            "is_at_home() is deprecated; returning False. "
            "Use MQTT telemetry for GPS proximity."
        )
        return False

    async def is_plugged_in(self) -> bool:
        """Check if vehicle is plugged in (deprecated).

        Deprecated: REST-based status polling is no longer used.
        The load manager relies on MQTT telemetry for vehicle state.
        This method always returns False.

        Returns:
            Always False.
        """
        logger.warning(
            "is_plugged_in() is deprecated; returning False. "
            "Use MQTT telemetry for plugged-in state."
        )
        return False

    async def get_charge_limit_pct(self) -> float | None:
        """Get target SOC percentage (deprecated).

        Deprecated: REST-based status polling is no longer used.
        The load manager does not read charge limits via REST.
        This method always returns None.

        Returns:
            Always None.
        """
        logger.warning(
            "get_charge_limit_pct() is deprecated; returning None. "
            "The load manager does not use charge limits."
        )
        return None

    async def start_charging(self) -> bool:
        """Start charging (deprecated — no-op).

        Deprecated: The load manager must not start charging vehicles.
        This method is a no-op that returns False and logs a warning.
        Charging must be initiated manually or via separate automation.
        """
        logger.warning(
            "start_charging() is disabled: the load manager must not "
            "start charging. Please start the car manually or via "
            "separate automation."
        )
        return False

    async def stop_charging(self) -> bool:
        """Send charge_stop command."""
        await self._ensure_api()
        if self._api is None:
            logger.error(
                "Cannot stop Tesla charging: API unavailable. "
            )
            return False
        if not self._api.has_private_key:
            logger.error(
                "Cannot stop Tesla charging: private key not loaded. "
                "Set TESLA_PRIVATE_KEY_PATH to a valid key file."
            )
            return False
        try:
            vehicle = await self._get_vehicle()
            await vehicle.charge_stop()
            logger.info("Tesla charge_stop sent successfully")
            self.save_tokens()
            return True
        except BaseException as e:
            if isinstance(e, (KeyboardInterrupt, SystemExit)):
                raise
            if _is_auth_error(e):
                raise TeslaAuthError(str(e)) from e
            logger.error("Failed to stop Tesla charging: %s", e)
            return False
        finally:
            await self.close()

    async def set_charge_amps(self, amps: int) -> bool:
        """Set charging amps within [min, max] range."""
        await self._ensure_api()
        if self._api is None:
            logger.error(
                "Cannot set Tesla charge amps: API unavailable. "
            )
            return False
        if not self._api.has_private_key:
            logger.error(
                "Cannot set Tesla charge amps: private key not loaded. "
                "Set TESLA_PRIVATE_KEY_PATH to a valid key file."
            )
            return False
        clamped = max(
            self.config.charge_amps_min, min(self.config.charge_amps_max, amps)
        )
        # Belt-and-suspenders: hard absolute max regardless of config.
        HARD_MAX_AMPS = 48
        clamped = min(clamped, HARD_MAX_AMPS)
        try:
            vehicle = await self._get_vehicle()
            await vehicle.set_charging_amps(clamped)
            logger.info("Tesla set_charge_amps(%d) sent successfully", clamped)
            self.save_tokens()
            return True
        except BaseException as e:
            if isinstance(e, (KeyboardInterrupt, SystemExit)):
                raise
            if _is_auth_error(e):
                raise TeslaAuthError(str(e)) from e
            logger.error("Failed to set Tesla charge amps: %s", e)
            return False
        finally:
            await self.close()

    async def get_charging_state(self) -> TeslaState | None:
        """Return full state from MQTT telemetry (deprecated).

        Deprecated: REST-based status polling is no longer used.
        The load manager relies solely on Tesla fleet-telemetry MQTT
        for vehicle state. This method always returns None.

        Returns:
            Always None.
        """
        logger.warning(
            "get_charging_state() is deprecated; returning None. "
            "Use MQTT telemetry for Tesla state."
        )
        return None

    async def init_tesla_state(self, timeout: int = 60) -> TeslaState | None:
        """Initialize Tesla state from telemetry or REST API fallback.

        Waits up to ``timeout`` seconds for MQTT telemetry to arrive.
        If telemetry provides the required fields (DetailedChargeState),
        converts the snapshot to a TeslaState and returns it immediately.

        If telemetry times out, falls back to the REST API:
        1. Use ChargeAmps from telemetry (if available) to determine
           is_charging and current_amps, skipping the charge_state REST call.
        2. If charging and home_lat/home_lon are configured, fetch drive_state
           to compute at_home via haversine distance.
        3. Build and return a TeslaState.

        The result is cached so this method only performs the init once.
        Subsequent calls return the cached state immediately.

        When ``_init_from_rest()`` fails (e.g. vehicle is offline), exponential
        backoff is applied: 30s, 60s, 120s, ..., capped at 900s (15 min). The
        telemetry fast path remains unaffected by backoff.

        Args:
            timeout: Maximum seconds to wait for telemetry before falling back to REST.

        Returns:
            TeslaState if available, or None if both telemetry and REST fail.
        """
        # Return cached result immediately
        if self._init_state is not None:
            return self._init_state

        # Backoff: skip REST fallback if we recently failed.
        # Telemetry (Phase 1) is always attempted — it is local and free.
        if self._last_init_attempt > 0:
            elapsed = _time.monotonic() - self._last_init_attempt
            if elapsed < self._backoff_secs:
                logger.debug(
                    "init_tesla_state: in backoff — skipping REST fallback, "
                    "%.1fs remaining of %.1fs backoff",
                    self._backoff_secs - elapsed, self._backoff_secs,
                )
                return None

        # Phase 1: Wait for telemetry
        from mqtt_telemetry import get_telemetry_snapshot, has_telemetry, tesla_state_from_snapshot

        waited = 0
        while waited < timeout:
            if has_telemetry():
                snapshot = get_telemetry_snapshot()

                state = tesla_state_from_snapshot(snapshot)
                if state is not None:
                    self._init_state = state
                    self._backoff_secs = 0.0
                    logger.info(
                        "init_tesla_state: telemetry arrived after %.1fs — is_charging=%s",
                        waited, state.is_charging,
                    )
                    return state
            await asyncio.sleep(1)
            waited += 1

        # Phase 2: Telemetry timed out — fall back to REST API
        # Pass the partial snapshot so _init_from_rest can use ChargeAmps
        # (if present) instead of making an unnecessary charge_state REST call.
        partial_snapshot = get_telemetry_snapshot() if has_telemetry() else {}
        logger.info(
            "init_tesla_state: telemetry not available after %ds, "
            "falling back to REST (snapshot keys: %s)",
            timeout, sorted(partial_snapshot.keys()),
        )
        self._last_init_attempt = _time.monotonic()
        state = await self._init_from_rest(snapshot=partial_snapshot)
        if state is not None:
            self._init_state = state
            self._backoff_secs = 0.0
        else:
            # Increase backoff: double, floor at CAR_OFFLINE_BACKOFF_SECS,
            # cap at CAR_OFFLINE_BACKOFF_MAX.
            self._backoff_secs = min(
                max(self._backoff_secs * 2, CAR_OFFLINE_BACKOFF_SECS),
                CAR_OFFLINE_BACKOFF_MAX,
            )
        return state

    async def _init_from_rest(
        self, snapshot: dict[str, Any] | None = None,
    ) -> TeslaState | None:
        """Fetch Tesla state from REST API as telemetry fallback.

        When ``snapshot`` contains ``ChargeAmps``, uses it to derive
        ``is_charging`` (amps > 0) and ``current_amps`` directly, skipping
        the ``charge_state`` REST call entirely — only ``drive_state`` is
        fetched (if home coords are configured).  This avoids an unnecessary
        API round-trip when telemetry already tells us the vehicle is charging.

        When ``snapshot`` is absent or empty, falls back to the original
        two-call strategy: ``charge_state`` for charging status and amps,
        then optionally ``drive_state`` for location.

        The Tesla Fleet API wraps endpoint data in a ``response`` key.
        Both wrapped (``{"response": {"charge_state": ...}}``) and
        unwrapped (``{"charge_state": ...}``) formats are accepted for
        backward compatibility.

        Returns:
            TeslaState built from REST data, or None on failure.
        """
        try:
            # ── Derive is_charging, current_amps, plugged_in ────────────────
            if snapshot and snapshot.get("ChargeAmps") is not None:
                # Telemetry snapshot has ChargeAmps — we already know the
                # vehicle is charging.  Skip the charge_state REST call.
                charge_amps_raw = snapshot["ChargeAmps"]
                raw_val = (
                    charge_amps_raw.get("value", charge_amps_raw)
                    if isinstance(charge_amps_raw, dict)
                    else charge_amps_raw
                )
                if raw_val is not None:
                    current_amps = round(float(raw_val))
                else:
                    current_amps = None
                is_charging = current_amps is not None and current_amps > 0
                plugged_in = is_charging
                logger.debug(
                    "_init_from_rest: using ChargeAmps=%s from snapshot — "
                    "skipping charge_state REST call",
                    current_amps,
                )
            else:
                # No ChargeAmps snapshot — fall back to full REST fetch.
                charge_data = await self._fetch_vehicle_data(endpoints=["charge_state"])
                cs = (
                    charge_data.get("response", {}).get("charge_state")
                    or charge_data.get("charge_state")
                )
                if cs is None:
                    logger.warning(
                        "_init_from_rest: no charge_state in tesla API response",
                    )
                    return None

                cs_str = cs.get("charging_state", "")
                is_charging = cs_str == "Charging"
                plugged_in = cs_str in ("Charging", "Complete", "PluggedIn")
                current_amps = cs.get("charge_amps")
                if current_amps is not None:
                    current_amps = int(current_amps)

            # ── Location from location_data (only if charging + home coords) ──
            at_home = False  # Assume not home until proven otherwise
            if is_charging and self.config.home_lat is not None and self.config.home_lon is not None:
                logger.debug(
                    "_init_from_rest: fetching location_data "
                    "(home_lat=%s, home_lon=%s)",
                    self.config.home_lat, self.config.home_lon,
                )
                try:
                    loc_data = await self._fetch_vehicle_data(endpoints=["location_data"])
                    loc = (
                        loc_data.get("response", {}).get("drive_state")
                        or loc_data.get("drive_state")
                    )
                    if loc is not None:
                        drv_lat = loc.get("latitude")
                        drv_lon = loc.get("longitude")
                        if drv_lat is not None and drv_lon is not None:
                            dist_m = _haversine_distance(
                                float(drv_lat), float(drv_lon),
                                float(self.config.home_lat), float(self.config.home_lon),
                            )
                            at_home = dist_m <= self.config.home_radius_m
                except Exception as e:
                    logger.warning("_init_from_rest: tesla API location_data fetch failed: %s", e)
            else:
                logger.debug(
                    "_init_from_rest: skipping location_data fetch "
                    "(is_charging=%s, home_lat=%s, home_lon=%s)",
                    is_charging, self.config.home_lat, self.config.home_lon,
                )

            # ── Build TeslaState ────────────────────────────────────────────
            self.save_tokens()
            return TeslaState(
                is_charging=is_charging,
                current_amps=current_amps,
                plugged_in=plugged_in,
                at_home=at_home,
            )

        except BaseException as e:
            # TeslaFleetError subclasses (e.g. VehicleOffline) inherit from
            # BaseException, NOT Exception, so ``except Exception`` misses them.
            if isinstance(e, (KeyboardInterrupt, SystemExit)):
                raise
            logger.warning("_init_from_rest: tesla REST API failed: %s", e)
            return None

    async def maybe_refresh_token(self) -> None:
        """Proactively refresh Tesla token if not refreshed in 7+ hours.

        This keeps the refresh token alive during idle periods when no Tesla
        API calls are being made (e.g. no charging needed).  The 7-hour window
        is deliberately shorter than the 8-hour access-token lifetime so that
        the refresh always happens before the access token expires server-side.
        """
        elapsed = _time.monotonic() - self._last_saved_tokens_at
        if elapsed < 7 * 3600:
            return  # recently refreshed, nothing to do

        await self._ensure_api()
        if self._api is None:
            return
        try:
            await self._api.refresh_access_token()
            self.save_tokens()
            logger.info(
                "Proactive Tesla token refresh succeeded (elapsed=%.1f h)",
                elapsed / 3600,
            )
        except BaseException as e:
            if isinstance(e, (KeyboardInterrupt, SystemExit)):
                raise
            if _is_auth_error(e):
                logger.warning(
                    "Proactive token refresh failed (auth error): %s", e,
                )
            else:
                logger.warning("Proactive token refresh failed: %s", e)

    async def close(self) -> None:
        """Close the aiohttp session."""
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    def reset_session(self) -> None:
        """Reset cached session and API client.

        Must be called before each asyncio.run() invocation, since asyncio.run()
        creates a fresh event loop and closes it afterward. A cached aiohttp
        ClientSession is bound to the loop it was created in and becomes
        unusable once that loop is closed. We discard the old session; Python's
        GC will clean it up.
        """
        self._session = None
        self._api = None
        self.last_error = None


async def tesla_auth_cli(config: Config | None = None) -> bool:
    """CLI helper for initial Tesla OAuth authentication.

    Prints the authorization URL and prompts for the callback code.
    Returns True on success, False on failure.

    Args:
        config: Optional Config instance. Falls back to module-level
            singleton when None.
    """
    from tesla_fleet_api import TeslaFleetOAuth

    cfg = config if config is not None else _cfg_mod._config
    client_id = cfg.tesla_client_id or ""
    if not client_id:
        print(
            "Error: TESLA_CLIENT_ID not set in environment. "
            "Set it before running --tesla-auth."
        )
        return False

    session = aiohttp.ClientSession()
    region = cast(Literal["na", "eu", "cn"], cfg.tesla_region)
    api = TeslaFleetOAuth(
        session=session,
        region=region,
        client_id=client_id,
        client_secret=cfg.tesla_client_secret or "",
        redirect_uri=cfg.tesla_redirect_uri,
    )

    try:
        login_url = api.get_login_url(scopes=["openid", "offline_access", "vehicle_device_data", "vehicle_cmds", "vehicle_charging_cmds", "vehicle_location"])  # type: ignore[list-item]
        print(f"Open this URL in your browser:\n{login_url}")
        print(
            "After authorizing, you'll be redirected to a URL containing 'code'.\n"
            "Copy the code parameter value and paste it below:"
        )
        code = input("Authorization code: ").strip()

        if not code:
            print("Error: No authorization code provided.")
            return False

        await api.get_refresh_token(code)
        refresh_token = api.refresh_token
        if refresh_token is None:
            print("Error: Failed to obtain refresh token.")
            return False

        # pylint: disable=protected-access
        # TeslaFleetOAuth exposes no public getter for access_token.
        save_tesla_tokens(
            refresh_token=refresh_token,
            access_token=api._access_token,  # type: ignore[arg-type, attr-defined]
            expires=api.expires,             # type: ignore[attr-defined]
        )
        print("\nSuccess! Tesla tokens saved.")
        return True
    finally:
        await session.close()


class RealPlugController(AbstractPlugController):
    """Controls HomeKit smart plugs via aiohomekit.

    Uses IpController to manage pairings persisted in `.homekit-pairings.json`.
    Each plug is identified by its accessory_id (IP address or mDNS name), which
    must match the key used during pairing with ``--pair-plug``. The controller
    discovers the On characteristic within the Switch service for each plug,
    then uses (aid, iid) tuples for get/put operations.
    """

    def __init__(
        self,
        plugs: dict[str, PlugConfig],
        pairings_path: Path = PAIRINGS_FILE,
    ) -> None:
        """Initialize controller with plug configs and pairing file path.

        Args:
            plugs: Mapping of plug name to configuration.
            pairings_path: Path to JSON file for persistent pairing data.
        """
        self.plugs = plugs
        self.pairings_path = pairings_path
        self._pairing: AbstractPairing | None = None
        self._connected = False
        self._on_char: tuple[int, int] | None = None

    def _load_pairing_data(self) -> dict[str, Any] | None:
        """Load pairing data from JSON file.

        Returns:
            Dict with pairing entries, or None if file doesn't exist.
        """
        if not self.pairings_path.exists():
            return None
        try:
            with open(self.pairings_path, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.error("Failed to load pairings file: %s", e)
            return None

    def _save_pairing_data(self, data: dict[str, Any]) -> None:
        """Save pairing data to JSON file."""
        try:
            with open(self.pairings_path, "w", encoding="utf-8") as f:
                json.dump(data, f)
        except OSError as e:
            logger.error("Failed to save pairings file: %s", e)

    async def connect(self) -> bool:
        """Load pairing data and establish connection to accessory.

        Returns:
            True if connected successfully, False otherwise.
        """
        from aiohomekit.controller.ip import IpController
        from aiohomekit.model.characteristics import CharacteristicsTypes
        from aiohomekit.model.services import ServicesTypes
        from zeroconf.asyncio import AsyncZeroconf

        pairing_data = self._load_pairing_data()
        if not pairing_data:
            logger.warning("No pairing data found at %s", self.pairings_path)
            return False

        target_ip = None
        for plug_config in self.plugs.values():
            target_ip = plug_config.accessory_id
            break

        if target_ip is None:
            logger.warning("No plugs configured")
            return False

        entry = pairing_data.get(target_ip)
        if entry is None:
            logger.error(
                "No pairing found for %s. Run --pair-plug first.", target_ip
            )
            return False

        azc = AsyncZeroconf()
        await azc.async_wait_for_start()  # type: ignore[attr-defined]

        try:
            controller = IpController(None, azc)  # type: ignore[arg-type]
        except RuntimeError as e:
            logger.error("Failed to create IpController: %s", e)
            return False

        try:
            pairing = controller.load_pairing(target_ip, entry)
        except Exception as e:
            logger.error("Failed to load pairing for %s: %s", target_ip, e)
            return False

        if pairing is None:
            logger.error(
                "Could not restore pairing for %s. Run --pair-plug first.",
                target_ip,
            )
            return False

        self._pairing = pairing

        try:
            accessories = pairing.list_accessories_and_characteristics()  # type: ignore[attr-defined]
        except Exception as e:
            logger.error("Failed to list accessories: %s", e)
            self._connected = False
            return False

        switch_uuid = ServicesTypes.SWITCH
        on_uuid = CharacteristicsTypes.ON

        for accessory in accessories:  # type: ignore[attr-defined]
            aid = accessory["aid"]
            for service in accessory.get("services", []):
                if service.get("sid") != switch_uuid:
                    continue
                for char in service.get("chars", []):
                    if char.get("cid") == on_uuid:
                        iid = char["iid"]
                        self._on_char = (aid, iid)
                        break
                if self._on_char:
                    break
            if self._on_char:
                break

        if self._on_char is None:
            logger.error(
                "No Switch/On characteristic found for %s", target_ip
            )
            return False

        try:
            await pairing.async_populate_accessories_state()
            self._connected = True
            logger.info("Connected to HomeKit accessory at %s", target_ip)
        except Exception as e:
            logger.error("Failed to connect to %s: %s", target_ip, e)
            self._connected = False

        return self._connected

    async def disconnect(self) -> None:
        """Close connection to the HomeKit accessory."""
        if self._pairing and self._connected:
            try:
                await self._pairing.close()
            except OSError as e:
                logger.warning("Error closing pairing: %s", e)
        self._connected = False

    async def get_state(self, name: str) -> bool | None:
        """Query HomeKit accessory on/off state.

        Args:
            name: Plug configuration name.

        Returns:
            True if on, False if off, None on error or not connected.
        """
        plug = self.plugs.get(name)
        if plug is None:
            logger.warning("Unknown plug %s", name)
            return None

        if not self._connected or self._pairing is None:
            if not await self.connect():
                return None

        assert self._pairing is not None

        if self._on_char is None:
            logger.error("On characteristic not discovered for %s", name)
            return None

        try:
            result = await self._pairing.get_characteristics([self._on_char])
            on_value = result.get(self._on_char, {}).get("value")
            if on_value is None:
                return None
            return bool(on_value)
        except OSError as e:
            logger.error("Connection error for plug %s: %s", name, e)
            self._connected = False
            return None

    async def set_state(self, name: str, on: bool) -> bool:
        """Turn HomeKit plug on or off.

        Args:
            name: Plug configuration name.
            on: Desired state.

        Returns:
            True on success, False on failure.
        """
        plug = self.plugs.get(name)
        if plug is None:
            logger.warning("Unknown plug %s", name)
            return False

        if not self._connected or self._pairing is None:
            if not await self.connect():
                return False

        assert self._pairing is not None

        if self._on_char is None:
            logger.error("On characteristic not discovered for %s", name)
            return False

        try:
            await self._pairing.put_characteristics(
                [(self._on_char[0], self._on_char[1], int(on))]
            )
            logger.info("RealPlugController.set_state(%s, %s)", name, on)
            return True
        except OSError as e:
            logger.error("Connection error for plug %s: %s", name, e)
            self._connected = False
            return False


# === VOCOlinc Plug Controller ===


class VocolincPlugController(AbstractPlugController):
    """Controls VOCOlinc smart plugs via the VOCOlinc API.

    Uses the vocolinc.py library with lazy initialization and asyncio.to_thread()
    wrapping to keep synchronous boto3 calls from blocking the event loop.
    """

    def __init__(
        self,
        plugs: dict[str, PlugConfig],
        username: str | None = None,
        password: str | None = None,
        config: Config | None = None,
    ) -> None:
        """Initialize controller with plug configs and VOCOlinc credentials.

        Args:
            plugs: Mapping of plug name to configuration.
            username: VOCOlinc account username. If None, read from
                VOCOLINC_USERNAME env var.
            password: VOCOlinc account password. If None, read from
                VOCOLINC_PASSWORD env var.
            config: Optional Config instance. Falls back to module-level
                singleton when None.
        """
        self.plugs = plugs
        self._username = username
        self._password = password
        self._cfg = config if config is not None else _cfg_mod._config
        self._client: Any | None = None
        self._initialized = False

    def _ensure_initialized(self) -> None:
        """Lazy-initialize the VOCOlinc client and login if needed."""
        if self._initialized:
            return

        username = self._username or self._cfg.vocolinc_username
        password = self._password or self._cfg.vocolinc_password

        if not username or not password:
            raise RuntimeError(
                "VOCOlinc credentials not configured. Set VOCOLINC_USERNAME "
                "and VOCOLINC_PASSWORD environment variables."
            )

        # Look up VOCOlinc via load_manager so that patch("load_manager.VOCOlinc")
        # intercepts it correctly in tests.  Also check sys.modules["vocolinc"]
        # first so that monkeypatch.setitem(sys.modules, "vocolinc", mock) works.
        _sys_voc = sys.modules.get("vocolinc")
        if isinstance(_sys_voc, MagicMock):
            VOCOlinc = _sys_voc.VOCOlinc  # type: ignore[attr-defined]
        else:
            _load_manager = sys.modules.get("load_manager")
            VOCOlinc = (
                getattr(_load_manager, "VOCOlinc", None)
                if _load_manager is not None
                else None
            )
        if VOCOlinc is None:
            from vocolinc import VOCOlinc  # type: ignore[assignment]

        assert VOCOlinc is not None, "VOCOlinc class could not be imported"
        client = VOCOlinc(username, password)
        client.login()
        self._client = client
        self._initialized = True
        logger.info(
            "VOCOlinc initialized with %d device(s)", len(client.devices)
        )

    async def get_state(self, name: str) -> bool | None:
        """Query VOCOlinc plug on/off state.

        Args:
            name: Plug configuration name.

        Returns:
            True if on, False if off, None on error.
        """
        plug = self.plugs.get(name)
        if plug is None:
            logger.warning("Unknown plug %s", name)
            return None

        try:
            self._ensure_initialized()
            assert self._client is not None
            device_name = plug.accessory_id
            result = await asyncio.to_thread(self._client.get_plug, device_name)
            return result
        except RuntimeError as e:
            logger.error("VOCOlinc initialization failed: %s", e)
            return None
        except Exception as e:
            logger.error("Failed to get state for plug %s: %s", name, e)
            return None

    async def set_state(self, name: str, on: bool) -> bool:
        """Turn VOCOlinc plug on or off.

        Args:
            name: Plug configuration name.
            on: Desired state.

        Returns:
            True on success, False on failure.
        """
        plug = self.plugs.get(name)
        if plug is None:
            logger.warning("Unknown plug %s", name)
            return False

        try:
            self._ensure_initialized()
            assert self._client is not None
            device_name = plug.accessory_id
            await asyncio.to_thread(
                self._client.set_plug, device_name, on
            )
            logger.info("VocolincPlugController.set_state(%s, %s)", name, on)
            return True
        except RuntimeError as e:
            logger.error("VOCOlinc initialization failed: %s", e)
            return False
        except Exception as e:
            logger.error("Failed to set state for plug %s: %s", name, e)
            return False


# === Composite Plug Controller ===


class CompositePlugController(AbstractPlugController):
    """Delegates plug operations to HomeKit or VOCOlinc backends.

    Merges plug configurations from both controller types and routes each
    operation to the correct backend based on the plug's controller_type.
    """

    def __init__(
        self,
        homekit_ctrl: AbstractPlugController,
        vocolinc_ctrl: AbstractPlugController,
    ) -> None:
        """Initialize with both backend controllers.

        Args:
            homekit_ctrl: Controller for HomeKit plugs.
            vocolinc_ctrl: Controller for VOCOlinc plugs.
        """
        self._homekit_ctrl = homekit_ctrl
        self._vocolinc_ctrl = vocolinc_ctrl
        self.plugs: dict[str, PlugConfig] = {}
        # Merge plugs from both controllers
        hk_plugs = getattr(homekit_ctrl, "plugs", {})
        vc_plugs = getattr(vocolinc_ctrl, "plugs", {})
        self.plugs.update(hk_plugs)
        self.plugs.update(vc_plugs)
        logger.info(
            "CompositePlugController initialized with %d HomeKit and "
            "%d VOCOlinc plug(s)",
            len(hk_plugs),
            len(vc_plugs),
        )

    async def get_state(self, name: str) -> bool | None:
        """Query plug state from the appropriate backend.

        Args:
            name: Plug configuration name.

        Returns:
            True if on, False if off, None on error.
        """
        plug = self.plugs.get(name)
        if plug is None:
            logger.warning("Unknown plug %s", name)
            return None

        if plug.controller_type == "vocolinc":
            return await self._vocolinc_ctrl.get_state(name)
        return await self._homekit_ctrl.get_state(name)

    async def set_state(self, name: str, on: bool) -> bool:
        """Turn plug on or off via the appropriate backend.

        Args:
            name: Plug configuration name.
            on: Desired state.

        Returns:
            True on success, False on failure.
        """
        plug = self.plugs.get(name)
        if plug is None:
            logger.warning("Unknown plug %s", name)
            return False

        if plug.controller_type == "vocolinc":
            return await self._vocolinc_ctrl.set_state(name, on)
        return await self._homekit_ctrl.set_state(name, on)


def pair_homekit_accessory(
    accessory_id: str,
    pin: str,
    pairings_path: Path = PAIRINGS_FILE,
) -> bool:
    """Perform initial HomeKit pairing for an accessory.

    This function is called from the CLI to set up a new pairing.
    It discovers the accessory via mDNS and performs the pairing dance.

    Args:
        accessory_id: IP address or mDNS name of the HomeKit accessory, as
            discovered by aiohomekit's async_discover(). Must match the value
            used in LOAD_PLUG_<NAME> env vars and the pairing file key.
        pin: Setup PIN code displayed on the accessory.
        pairings_path: Path to JSON file for persistent pairing data.

    Returns:
        True if pairing succeeded, False otherwise.
    """
    from aiohomekit.controller.ip import IpController
    from zeroconf.asyncio import AsyncZeroconf

    azc = AsyncZeroconf()

    try:
        controller = IpController(None, azc)  # type: ignore[arg-type]
    except RuntimeError as e:
        logger.error("Failed to create IpController for pairing: %s", e)
        return False

    async def _do_pair() -> bool:
        try:
            await controller.async_start()
            discoveries = [d async for d in controller.async_discover()]  # type: ignore[misc]
            target = None
            for disc in discoveries:
                if accessory_id in (disc.address, disc.name):  # type: ignore[attr-defined]
                    target = disc
                    break
            if target is None:
                logger.error(
                    "Accessory at %s not found via mDNS discovery", accessory_id
                )
                return False

            try:
                finish_pairing = await target.async_start_pairing(accessory_id)
                pairing = await finish_pairing(pin)
            except Exception as e:
                logger.error("Pairing failed for %s: %s", accessory_id, e)
                return False

            existing = {}
            if pairings_path.exists():
                try:
                    with open(pairings_path, encoding="utf-8") as f:
                        existing = json.load(f)
                except (json.JSONDecodeError, OSError):
                    existing = {}

            existing[accessory_id] = pairing.pairing_data  # type: ignore[attr-defined]

            try:
                with open(pairings_path, "w", encoding="utf-8") as f:
                    json.dump(existing, f)
            except OSError as e:
                logger.error("Failed to save pairings file: %s", e)
                return False

            logger.info("Successfully paired HomeKit accessory at %s", accessory_id)
            return True
        finally:
            await controller.async_stop()
            await azc.async_close()

    return asyncio.run(_do_pair())


async def fleet_telemetry_config_create(
    ctrl: "RealTeslaController",
    config: FleetTelemetryProvisionConfig,
) -> None:
    """Call the Tesla Fleet API fleet_telemetry_config_create endpoint.

    Args:
        ctrl: An authenticated RealTeslaController instance.
        config: Provisioning parameters (hostname, CA cert, intervals).

    Raises:
        RuntimeError: If the API is not authenticated.
        Exception: On API or file-I/O failure.
    """
    await ctrl._ensure_api()
    await ctrl.authenticate()
    assert ctrl._api is not None
    assert ctrl._vin is not None
    assert ctrl._vin != ""
    ca_cert = config.ca_file_path.read_text()
    body = {
        "vins": [ctrl._vin],
        "config": {
            "hostname": config.server_hostname,
            "port": config.server_port,
            "ca": ca_cert,
            "fields": {
                "DetailedChargeState": {"interval_seconds": config.detailedchargestate_interval_sec},
                "ChargeAmps": {"interval_seconds": config.chargeamps_interval_sec},
                "ChargeState": {"interval_seconds": config.chargestate_interval_sec},
                "Location": {"interval_seconds": config.location_interval_sec},
            },
            "alert_types": ["service"]
        }
    }
    fleet = ctrl._api.vehicles.Fleet(ctrl._api, ctrl._vin)  # type: ignore[union-attr]
    await fleet.fleet_telemetry_config_create(body)
    logger.info(
        "fleet_telemetry_config_create: registered hostname=%s", config.server_hostname
    )
