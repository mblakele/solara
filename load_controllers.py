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
from pathlib import Path
from typing import Any, cast, Literal

import sys

import aiohttp
from aiohomekit.controller.abstract import AbstractPairing

import config as _cfg_mod


from load_models import (
    AbstractPlugController,
    AbstractTeslaController,
    PlugAction,
    PlugConfig,
    TeslaAuthError,
    TeslaConfig,
    TeslaState,
)


logger = logging.getLogger(__name__)


# Default path for pairing persistence
PAIRINGS_FILE = Path(".homekit-pairings.json")

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
        self.action_log.append(PlugAction(name=name, on=on))
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
            soc_percent=50.0,
            plugged_in=False,
            at_home=True,
            at_charge_limit=False,
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

    async def get_charge_limit_pct(self) -> float | None:
        """Return stored SOC percentage."""
        if self._state.at_charge_limit:
            return None
        return self._state.soc_percent

    async def is_at_charge_limit(self) -> bool:
        """Return stored at_charge_limit flag."""
        return self._state.at_charge_limit

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
            soc_percent=self._state.soc_percent,
            plugged_in=self._state.plugged_in,
            at_home=self._state.at_home,
            at_charge_limit=self._state.at_charge_limit,
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

    def __init__(self, tesla_config: TeslaConfig) -> None:
        self.config = tesla_config
        self.last_error: str | None = None
        self._session: aiohttp.ClientSession | None = None
        self._api: Any | None = None  # TeslaFleetOAuth instance
        self._vin: str = ""

    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create the aiohttp session."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def _ensure_api(self) -> None:
        """Initialize the TeslaFleetOAuth API client if needed.

        Reloads persisted tokens from disk on each call so that tokens written
        by an external OAuth callback are picked up without restarting gunicorn.
        """
        from tesla_fleet_api import TeslaFleetOAuth

        session = await self._get_session()
        region = cast(Literal["na", "eu", "cn"], _cfg_mod.cfg.tesla_region)

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

        region = _cfg_mod.cfg.tesla_region
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
        """Check vehicle GPS against home lat/lon using haversine distance."""
        try:
            data = await self._fetch_vehicle_data(["drive_state", "location_data"])
            response = data.get("response", {})
            drive_state = response.get("drive_state", {})
            location_data = response.get("location_data", {})
            lat = location_data.get("latitude") or drive_state.get("latitude") or drive_state.get("native_latitude")
            lon = location_data.get("longitude") or drive_state.get("longitude") or drive_state.get("native_longitude")


            if lat is None or lon is None:
                logger.warning(
                    "Tesla location unavailable (lat=%s, lon=%s)", lat, lon
                )
                return False

            distance = _haversine_distance(
                self.config.home_lat,
                self.config.home_lon,
                lat,
                lon,
            )
            is_home = distance <= self.config.home_radius_m
            logger.debug(
                "Tesla vehicle %s at home (distance=%.1fm)",
                "is" if is_home else "is not",
                distance,
            )
            return is_home
        except BaseException as e:
            if isinstance(e, (KeyboardInterrupt, SystemExit)):
                raise
            logger.error("Failed to check Tesla location: %s", e)
            return False

    async def is_plugged_in(self) -> bool:
        """Check chargeState indicates plugged in (not Disconnected)."""
        try:
            data = await self._fetch_vehicle_data(["charge_state"])
            charge_state = data.get("response", {}).get("charge_state", {})
            charging_state = charge_state.get("charging_state", "")
            return charging_state not in ("Disconnected", "")
        except BaseException as e:
            if isinstance(e, (KeyboardInterrupt, SystemExit)):
                raise
            logger.error("Failed to check Tesla plugged-in status: %s", e)
            return False

    async def get_charge_limit_pct(self) -> float | None:
        """Get target SOC percentage.

        Returns:
            Target SOC as 0-100, or None if at limit or error.
        """
        try:
            data = await self._fetch_vehicle_data(["charge_state"])
            charge_state = data.get("response", {}).get("charge_state", {})
            soc = charge_state.get("battery_level")
            charge_limit_soc = charge_state.get("charge_limit_soc")

            if soc is not None and charge_limit_soc is not None:
                if soc >= charge_limit_soc:
                    return None  # At limit
            return float(charge_limit_soc) if charge_limit_soc is not None else None
        except BaseException as e:
            if isinstance(e, (KeyboardInterrupt, SystemExit)):
                raise
            logger.error("Failed to get Tesla charge limit: %s", e)
            return None

    async def is_at_charge_limit(self) -> bool:
        """Check if vehicle has reached its charge limit (saturated load)."""
        try:
            data = await self._fetch_vehicle_data(["charge_state"])
            charge_state = data.get("response", {}).get("charge_state", {})
            soc = charge_state.get("battery_level")
            charge_limit_soc = charge_state.get("charge_limit_soc")

            if soc is not None and charge_limit_soc is not None:
                return soc >= charge_limit_soc
            return False
        except BaseException as e:
            if isinstance(e, (KeyboardInterrupt, SystemExit)):
                raise
            logger.error("Failed to check Tesla charge limit: %s", e)
            return False

    async def start_charging(self) -> bool:
        """Send charge_start command."""
        if self._api is None or not self._api.has_private_key:
            logger.error(
                "Cannot start Tesla charging: private key not loaded. "
                "Set TESLA_PRIVATE_KEY_PATH to a valid key file."
            )
            return False
        try:
            vehicle = await self._get_vehicle()
            await vehicle.charge_start()
            logger.info("Tesla charge_start sent successfully")
            return True
        except BaseException as e:
            if isinstance(e, (KeyboardInterrupt, SystemExit)):
                raise
            logger.error("Failed to start Tesla charging: %s", e)
            return False
        finally:
            await self.close()

    async def stop_charging(self) -> bool:
        """Send charge_stop command."""
        if self._api is None or not self._api.has_private_key:
            logger.error(
                "Cannot stop Tesla charging: private key not loaded. "
                "Set TESLA_PRIVATE_KEY_PATH to a valid key file."
            )
            return False
        try:
            vehicle = await self._get_vehicle()
            await vehicle.charge_stop()
            logger.info("Tesla charge_stop sent successfully")
            return True
        except BaseException as e:
            if isinstance(e, (KeyboardInterrupt, SystemExit)):
                raise
            logger.error("Failed to stop Tesla charging: %s", e)
            return False
        finally:
            await self.close()

    async def set_charge_amps(self, amps: int) -> bool:
        """Set charging amps within [min, max] range."""
        if self._api is None or not self._api.has_private_key:
            logger.error(
                "Cannot set Tesla charge amps: private key not loaded. "
                "Set TESLA_PRIVATE_KEY_PATH to a valid key file."
            )
            return False
        clamped = max(
            self.config.charge_amps_min, min(self.config.charge_amps_max, amps)
        )
        try:
            vehicle = await self._get_vehicle()
            await vehicle.set_charging_amps(clamped)
            logger.info("Tesla set_charge_amps(%d) sent successfully", clamped)
            return True
        except BaseException as e:
            if isinstance(e, (KeyboardInterrupt, SystemExit)):
                raise
            logger.error("Failed to set Tesla charge amps: %s", e)
            return False
        finally:
            await self.close()

    async def get_charging_state(self) -> TeslaState | None:
        """Return full state: charging_bool, current_amps, soc_pct, plugged_in."""
        try:
            data = await self._fetch_vehicle_data(
                ["charge_state", "drive_state", "location_data"]
            )
            response = data.get("response", {})
            charge_state = response.get("charge_state", {})
            drive_state = response.get("drive_state", {})
            location_data = response.get("location_data", {})   # add this
            lat = (location_data.get("latitude")
                   or drive_state.get("latitude")
                   or drive_state.get("native_latitude"))
            lon = (location_data.get("longitude")
                   or drive_state.get("longitude")
                   or drive_state.get("native_longitude"))

            charging_state = charge_state.get("charging_state", "")
            is_charging = charging_state == "Charging"

            charge_amps = charge_state.get("charge_current_request")
            if charge_amps is None:
                charge_amps = charge_state.get("charger_pilot_current")
            current_amps: int | None = (
                int(charge_amps) if charge_amps is not None else None
            )

            soc_percent: float | None = (
                float(charge_state["battery_level"])
                if charge_state.get("battery_level") is not None
                else None
            )

            plugged_in = charging_state not in ("Disconnected", "")

            if lat is not None and lon is not None:
                distance = _haversine_distance(
                    self.config.home_lat,
                    self.config.home_lon,
                    lat,
                    lon,
                )
                at_home = distance <= self.config.home_radius_m
                logger.debug(
                    "Tesla location: lat=%.6f lon=%.6f distance=%.1fm radius=%.1fm at_home=%s",
                    lat,
                    lon,
                    distance,
                    self.config.home_radius_m,
                    at_home,
                )
            else:
                # GPS unavailable (missing vehicle_location scope or privacy mode).
                # If the car is plugged in it must be at the home charger.
                at_home = plugged_in
                logger.debug(
                    "Tesla location unavailable: drive_state keys=%s, "
                    "falling back to plugged_in=%s for at_home",
                    list(drive_state.keys()),
                    plugged_in,
                )

            charge_limit_soc = charge_state.get("charge_limit_soc")
            if soc_percent is not None and charge_limit_soc is not None:
                at_charge_limit = soc_percent >= float(charge_limit_soc)
            else:
                at_charge_limit = False

            self.last_error = None  # Clear any previous error on success
            return TeslaState(
                is_charging=is_charging,
                current_amps=current_amps,
                soc_percent=soc_percent,
                plugged_in=plugged_in,
                at_home=at_home,
                at_charge_limit=at_charge_limit,
            )
        except BaseException as e:
            # TeslaFleetError inherits from BaseException, not Exception.
            # Re-raise system-exiting exceptions, log and suppress others.
            if isinstance(e, (KeyboardInterrupt, SystemExit)):
                raise
            if "login_required" in str(e) or "refresh_token" in str(e) or "not authorized" in str(e):
                raise TeslaAuthError(str(e)) from e
            try:
                from tesla_fleet_api.exceptions import VehicleOffline
                if isinstance(e, VehicleOffline):
                    logger.info(
                        "Tesla vehicle is offline (asleep or unplugged); skipping state fetch"
                    )
                    return None
            except ImportError:
                pass
            raise
        finally:
            await self.close()

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


async def tesla_auth_cli() -> bool:
    """CLI helper for initial Tesla OAuth authentication.

    Prints the authorization URL and prompts for the callback code.
    Returns True on success, False on failure.
    """
    from tesla_fleet_api import TeslaFleetOAuth

    client_id = _cfg_mod.cfg.tesla_client_id or ""
    if not client_id:
        print(
            "Error: TESLA_CLIENT_ID not set in environment. "
            "Set it before running --tesla-auth."
        )
        return False

    session = aiohttp.ClientSession()
    region = cast(Literal["na", "eu", "cn"], _cfg_mod.cfg.tesla_region)
    api = TeslaFleetOAuth(
        session=session,
        region=region,
        client_id=client_id,
        client_secret=_cfg_mod.cfg.tesla_client_secret or "",
        redirect_uri=_cfg_mod.cfg.tesla_redirect_uri,
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
    ) -> None:
        """Initialize controller with plug configs and VOCOlinc credentials.

        Args:
            plugs: Mapping of plug name to configuration.
            username: VOCOlinc account username. If None, read from
                VOCOLINC_USERNAME env var.
            password: VOCOlinc account password. If None, read from
                VOCOLINC_PASSWORD env var.
        """
        self.plugs = plugs
        self._username = username
        self._password = password
        self._client: Any | None = None
        self._initialized = False

    def _ensure_initialized(self) -> None:
        """Lazy-initialize the VOCOlinc client and login if needed."""
        if self._initialized:
            return

        username = self._username or _cfg_mod.cfg.vocolinc_username
        password = self._password or _cfg_mod.cfg.vocolinc_password

        if not username or not password:
            raise RuntimeError(
                "VOCOlinc credentials not configured. Set VOCOLINC_USERNAME "
                "and VOCOLINC_PASSWORD environment variables."
            )

        # Look up VOCOlinc via load_manager so that patch("load_manager.VOCOlinc")
        # intercepts it correctly in tests.
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
